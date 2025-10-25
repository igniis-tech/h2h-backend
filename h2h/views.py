
# views.py
import hmac
import hashlib
import json
from datetime import date

from django.conf import settings
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.contrib.auth import login
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.db.models import Prefetch
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from collections import defaultdict
from django.db import transaction
import logging
from .models import (
    Package,
    Order,
    WebhookEvent,
    Property,
    UnitType,
    Unit,
    Booking,
    Allocation,
    Event,
    EventDay,
    PromoCode, 
)
from .serializers import (
    PackageSerializer,
    OrderSerializer,
    UserSerializer,
    PropertySerializer,
    UnitTypeSerializer,
    UnitSerializer,
    BookingSerializer,
    # events
    EventSerializer,
    EventDaySerializer,
)
from .auth_utils import (
    build_authorize_url,
    exchange_code_for_tokens,
    fetch_userinfo,
    get_or_create_user_from_userinfo,
)
from .pdf import build_invoice_and_pass_pdf_from_order
from h2h import models
logger = logging.getLogger("h2h.create_booking")
@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def api_docs(request):
    """
    Renders the interactive API documentation (index.html).
    Also sets a CSRF cookie so same-origin POST tests can work if you add them later.
    """
    # If your app is mounted at /api/, this makes the default BASE = /api/
    # If mounted at root, change to "/" or leave blank.
    default_base = request.build_absolute_uri(request.path)
    # Ensure trailing slash for ${BASE}
    if not default_base.endswith("/"):
        default_base += "/"
    return render(request, "index.html", {"default_base": default_base})


# -----------------------------------
# Health
# -----------------------------------
@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def health(request):
    return Response({"ok": True})

def _allowed_utype_ids_for_package(pkg: Package) -> list[int]:
    # First try M2M; else fallback map by name
    ids = list(pkg.allowed_unit_types.values_list("id", flat=True))
    if ids:
        return ids
    names = _allowed_unit_types_for_package(pkg)  # uses your fallback dict
    if not names:
        return []
    return list(UnitType.objects.filter(name__in=names).values_list("id", flat=True))

def _candidate_units_for_package(event: Event, pkg: Package, *, category: str | None,
                                 booking: Booking | None = None, lock: bool = False):
    """
    All FREE units inside this event for the given package. If booking already
    has property/unit_type, we keep that as a hard constraint, else we search across all.
    """
    taken_ids = _units_taken_for_event(event).values_list("id", flat=True)
    qs = Unit.objects.all()

    # constrain by booking if specified
    if booking and booking.property_id:
        qs = qs.filter(property_id=booking.property_id)
    if booking and booking.unit_type_id:
        qs = qs.filter(unit_type_id=booking.unit_type_id)
    else:
        allowed_ids = _allowed_utype_ids_for_package(pkg)
        qs = qs.filter(unit_type_id__in=allowed_ids or [-1])  # none if empty

    if category:
        qs = qs.filter(category=category)

    qs = qs.exclude(id__in=taken_ids)

    # we only want actually open stock
    qs = qs.filter(status="AVAILABLE")

    if lock:
        qs = qs.select_for_update()

    # prefer bigger capacity to reduce splits, then stable, human-friendly
    return qs.order_by("-capacity", "property__name", "unit_type__name", "label")


def _has_capacity_for_package(event: Event, pkg: Package, needed: int, category: str | None) -> bool:
    qs = _candidate_units_for_package(event, pkg, category=category, booking=None, lock=False)
    cap = 0
    for u in qs.iterator(chunk_size=500):
        cap += (u.capacity or 1)
        if cap >= max(1, needed):
            return True
    return False


# -----------------------------------
# SSO
# -----------------------------------
@api_view(["GET"])
@permission_classes([AllowAny])
def sso_authorize(request):
    state = request.GET.get("state", "")
    url = build_authorize_url(state)
    return Response({"authorization_url": url, "state": state, "provider": "cognito"})


@api_view(["GET"])
@permission_classes([AllowAny])
def sso_callback(request):
    code = request.GET.get("code")
    state = request.GET.get("state")
    if not code:
        return Response({"error": "missing code"}, status=400)
    try:
        tokens = exchange_code_for_tokens(code)
        access_token = tokens.get("access_token")
        if not access_token:
            return Response({"error": "no access_token"}, status=400)
        info = fetch_userinfo(access_token)
        user = get_or_create_user_from_userinfo(info)
        login(request, user)
        return Response(
            {
                "user": UserSerializer(user).data,
                "tokens": {k: v for k, v in tokens.items() if k in ("id_token", "access_token")},
                "claims": info,
                "state": state,
            }
        )
    except Exception as e:
        return Response({"error": str(e)}, status=400)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(UserSerializer(request.user).data)


# -----------------------------------
# Packages
# -----------------------------------

@api_view(["GET"])
@permission_classes([AllowAny])
def list_packages(request):
    """
    Returns:
    {
      "event": { ... event fields ..., "days": [ ... ] } | null,
      "packages": [ ... ]
    }

    Optional query params:
      - event_slug=...   -> pick specific event by slug
      - event_year=2025  -> pick specific event by year
    Fallback: the most relevant event with active=True & booking_open=True.
    """
    # -- packages --
    packages_qs = Package.objects.filter(active=True).order_by("price_inr")
    packages_data = PackageSerializer(packages_qs, many=True).data

    # -- event selection (optional filters) --
    event_slug = request.query_params.get("event_slug")
    event_year = request.query_params.get("event_year")

    event_qs = Event.objects.all()

    if event_slug:
        event_qs = event_qs.filter(slug=event_slug)
    elif event_year:
        event_qs = event_qs.filter(year=event_year)
    else:
        # default to current bookable event; fallback to latest active
        event_qs = event_qs.filter(active=True, booking_open=True)
        if not event_qs.exists():
            event_qs = Event.objects.filter(active=True)

    # Prefetch days in defined ordering (Meta.ordering on EventDay also applies)
    event_qs = event_qs.prefetch_related(
        Prefetch("days", queryset=EventDay.objects.all())
    ).order_by("-year", "-start_date")

    event_obj = event_qs.first()
    event_data = EventSerializer(event_obj).data if event_obj else None

    return Response({
        "event": event_data,
        "packages": packages_data,
    })


# -----------------------------------
# Razorpay helpers
# -----------------------------------
def _get_razorpay_client():
    """
    Lazily import and return a configured Razorpay client.
    Raises RuntimeError with a clear message if the SDK or keys are missing.
    """
    try:
        import razorpay
    except Exception:
        raise RuntimeError("Razorpay SDK is not installed on the server.")

    key_id = getattr(settings, "RAZORPAY_KEY_ID", None)
    key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", None)
    if not key_id or not key_secret:
        raise RuntimeError("Razorpay keys are not configured (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET).")

    return razorpay.Client(auth=(key_id, key_secret))


# -----------------------------------
# Package ↔ UnitType mapping (room/swiss/tent)
# -----------------------------------
# Default mapping (used if Package.allowed_unit_types M2M is not defined or empty):
#   room  -> COTTAGE, HUT
#   swiss -> SWISS TENT
#   tent  -> DOME TENT
PACKAGE_UNITTYPE_MAP = {
    "room": {"COTTAGE", "HUT"},
    "swiss": {"SWISS TENT"},
    "tent": {"DOME TENT"},
}

def _allowed_unit_types_for_package(package: Package):
    # DB-driven mapping, if you later add M2M
    if hasattr(package, "allowed_unit_types"):
        try:
            qs = package.allowed_unit_types.all()
            if qs.exists():
                return {ut.name.upper() for ut in qs}
        except Exception:
            pass
    # Fallback by name
    key = (package.name or "").strip().lower()
    return PACKAGE_UNITTYPE_MAP.get(key, set())

def _validate_package_vs_unit_type(package: Package, unit_type: UnitType):
    allowed = _allowed_unit_types_for_package(package)
    if not allowed:
        return
    if unit_type.name.upper() not in allowed:
        raise ValueError(
            f"Selected unit type '{unit_type.name}' is not allowed for package '{package.name}'. "
            f"Allowed: {', '.join(sorted(allowed))}"
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_bookings(request):
    """
    Returns the authenticated user's bookings (latest first),
    including nested order summary & pricing/promo snapshots.
    """
    qs = (Booking.objects
          .filter(user=request.user)
          .select_related("order", "event", "property", "unit_type")
          .order_by("-created_at"))
    return Response(BookingSerializer(qs, many=True).data)

# -----------------------------------
# Pricing helpers
# -----------------------------------
# def _compute_booking_pricing(package: Package, booking: Booking):
    """
    Compute total INR for a booking:
      - Base price covers 'base_includes' people.
      - Extra ADULT: extra_price_adult_inr (or base price if 0).
      - Child <= child_free_max_age: free
      - Child <= child_half_max_age: child_half_multiplier * extra adult price
    Guests can be provided either as explicit counts or as ages list.
    """
    base_includes = package.base_includes or 1
    base_price = int(package.price_inr)
    extra_adult_price = int(package.extra_price_adult_inr or base_price)
    half_mult = package.child_half_multiplier or 0.5
    free_max = package.child_free_max_age or 5
    half_max = package.child_half_max_age or 15
    if half_max < free_max:
        half_max = free_max  # guard

    # derive counts
    extra_adults = booking.extra_adults or 0
    child_half = booking.extra_children_half or 0
    child_free = booking.extra_children_free or 0

    # If ages are provided, override counts based on server-side rules
    ages = booking.guest_ages or []
    if isinstance(ages, list) and ages:
        a = h = f = 0
        for age in ages:
            try:
                age = int(age)
            except Exception:
                continue
            if age <= free_max:
                f += 1
            elif age <= half_max:
                h += 1
            else:
                a += 1
        extra_adults, child_half, child_free = a, h, f

    # Sanity: guests = base_includes + extras
    extras_total = max(0, extra_adults + child_half + child_free)
    guests_total = max(base_includes, booking.guests or (base_includes + extras_total))
    if guests_total < base_includes + extras_total:
        guests_total = base_includes + extras_total

    # charges
    child_half_price = int(round(extra_adult_price * float(half_mult)))
    extras_amount = (extra_adults * extra_adult_price) + (child_half * child_half_price)  # free pays 0
    total_inr = base_price + extras_amount

    breakdown = {
        "base": {"includes": base_includes, "price_inr": base_price},
        "extra_unit_prices": {
            "adult_inr": extra_adult_price,
            "child_half_inr": child_half_price,
            "child_free_inr": 0,
        },
        "extra_counts": {
            "adult": extra_adults,
            "child_half": child_half,
            "child_free": child_free,
        },
        "extras_amount_inr": extras_amount,
        "total_inr": total_inr,
        "rules": {
            "child_free_max_age": free_max,
            "child_half_max_age": half_max,
            "child_half_multiplier": half_mult,
        },
    }

    return total_inr, breakdown, guests_total


def _compute_booking_pricing(package: Package, booking: Booking):
    """
    Compute total INR for a booking:
      - Base price covers 'base_includes' people (per stay).
      - Extra ADULT: extra_price_adult_inr (or base price if 0).
      - Child <= child_free_max_age: free
      - Child <= child_half_max_age: child_half_multiplier * extra adult price

    Priority for deriving extras (server-side canonical):
      1) booking.companions  (derive entire party → allocate base seats → extras)
      2) booking.guest_ages  (interpreted as EXTRAS ONLY, like your current logic)
      3) booking.extra_*     (manual counts)

    Returns: (total_inr, breakdown_dict, guests_total)
    """
    # ---- package rules ----
    base_includes = int(package.base_includes or 1)
    base_price = int(package.price_inr)
    extra_adult_price = int(package.extra_price_adult_inr or base_price)
    half_mult = float(package.child_half_multiplier or 0.5)
    free_max = int(package.child_free_max_age or 5)
    half_max = int(package.child_half_max_age or 15)
    if half_max < free_max:
        half_max = free_max  # guard

    def _as_int_or_none(x):
        try:
            v = int(x)
            return max(0, min(120, v))
        except Exception:
            return None

    def _classify(age):
        # Unknown age => treat as ADULT for pricing safety
        if age is None:
            return "adult"
        if age <= free_max:
            return "free"
        if age <= half_max:
            return "half"
        return "adult"

    # ---------- PATH 1: companions present → derive whole party ----------
    companions = getattr(booking, "companions", None)
    used_source = None
    if isinstance(companions, list) and companions:
        used_source = "companions"

        # party size = primary user + companions
        guests_total = 1 + len(companions)

        # assemble ages for everyone (primary age unknown => adult)
        ages_everyone = [None]  # primary user
        for c in companions:
            age = _as_int_or_none((c or {}).get("age"))
            ages_everyone.append(age)

        # classify
        adults = [a for a in ages_everyone if _classify(a) == "adult"]
        halfs  = [a for a in ages_everyone if _classify(a) == "half"]
        frees  = [a for a in ages_everyone if _classify(a) == "free"]

        # allocate base seats to most expensive first
        remaining_base = min(base_includes, guests_total)
        alloc_adults = min(len(adults), remaining_base); remaining_base -= alloc_adults
        alloc_halfs  = min(len(halfs),  remaining_base); remaining_base -= alloc_halfs
        alloc_frees  = min(len(frees),  remaining_base); remaining_base -= alloc_frees

        # extras are those left after base allocation
        extra_adults = max(0, len(adults) - alloc_adults)
        child_half   = max(0, len(halfs)  - alloc_halfs)
        child_free   = max(0, len(frees)  - alloc_frees)

    else:
        # ---------- PATH 2: guest_ages provided (EXTRAS ONLY, legacy behavior) ----------
        ages = booking.guest_ages or []
        if isinstance(ages, list) and ages:
            used_source = "guest_ages"
            a = h = f = 0
            for age in ages:
                age = _as_int_or_none(age)
                if age is None:
                    # unknown => treat as adult
                    a += 1
                elif age <= free_max:
                    f += 1
                elif age <= half_max:
                    h += 1
                else:
                    a += 1
            extra_adults, child_half, child_free = a, h, f

            # guests_total must be ≥ base_includes + extras
            extras_total = max(0, extra_adults + child_half + child_free)
            guests_total = max(base_includes + extras_total, int(booking.guests or (base_includes + extras_total)))
        else:
            # ---------- PATH 3: fallback to manual counts ----------
            used_source = "manual_counts"
            extra_adults = int(booking.extra_adults or 0)
            child_half   = int(booking.extra_children_half or 0)
            child_free   = int(booking.extra_children_free or 0)
            extras_total = max(0, extra_adults + child_half + child_free)
            guests_total = max(base_includes + extras_total, int(booking.guests or (base_includes + extras_total)))

    # ---- price math ----
    child_half_price = int(round(extra_adult_price * half_mult))
    extras_amount = (int(extra_adults) * extra_adult_price) + (int(child_half) * child_half_price)  # free pays 0
    total_inr = base_price + extras_amount

    # Optional: if pricing should be per-night, multiply by booking.nights here
    # total_inr *= max(1, getattr(booking, "nights", 1))

    # ---- breakdown (keeps your original keys; adds 'allocation' + 'computed_from') ----
    breakdown = {
        "base": {"includes": base_includes, "price_inr": base_price},
        "extra_unit_prices": {
            "adult_inr": extra_adult_price,
            "child_half_inr": child_half_price,
            "child_free_inr": 0,
        },
        "extra_counts": {
            "adult": int(extra_adults),
            "child_half": int(child_half),
            "child_free": int(child_free),
        },
        "extras_amount_inr": extras_amount,
        "total_inr_before_promo": total_inr,   # for UIs that show discount line
        "total_inr": total_inr,
        "rules": {
            "child_free_max_age": free_max,
            "child_half_max_age": half_max,
            "child_half_multiplier": half_mult,
        },
        "computed_from": used_source,
    }

    # If companions path was used, surface the base-seat allocation for transparency
    if used_source == "companions":
        breakdown["allocation"] = {
            "base_seats_applied": {
                "adult": alloc_adults,
                "child_half": alloc_halfs,
                "child_free": alloc_frees,
            }
        }

    return total_inr, breakdown, guests_total


# -----------------------------------
# Payments
# -----------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_order(request):
    """
    Optionally accept booking_id to compute final amount including extra guests.
    Body:
    {
      "package_id": 1,
      "booking_id": 123,     # optional but recommended
    }
    """
    package_id = request.data.get("package_id")
    booking_id = request.data.get("booking_id")

    if not package_id:
        return Response({"error": "package_id required"}, status=400)

    try:
        package = Package.objects.get(id=package_id, active=True)
    except Package.DoesNotExist:
        return Response({"error": "invalid package"}, status=404)

    # Default: single person base price
    total_inr = int(package.price_inr)

    # If booking provided, compute pricing from server-side rules
    booking = None
    if booking_id:
        try:
            booking = Booking.objects.get(id=booking_id, user=request.user)
        except Booking.DoesNotExist:
            return Response({"error": "invalid booking_id"}, status=400)

        # Validate package vs chosen unit_type
        try:
            _validate_package_vs_unit_type(package, booking.unit_type)
        except ValueError as ve:
            return Response({"error": str(ve)}, status=400)

        total_inr, breakdown, guests_total = _compute_booking_pricing(package, booking)
        # snapshot onto booking
        booking.pricing_total_inr = total_inr
        booking.pricing_breakdown = breakdown
        booking.guests = guests_total
        booking.save(update_fields=["pricing_total_inr", "pricing_breakdown", "guests"])

    amount_paise = int(total_inr) * 100

    try:
        client = _get_razorpay_client()
    except RuntimeError as e:
        return Response({"error": str(e)}, status=503)

    # (A) Create Razorpay order
    try:
        rp_order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "payment_capture": 1,
            "notes": {"package": package.name, "booking_id": booking_id or ""},
        })
    except Exception as e:
        return Response({"error": f"Failed to create Razorpay order: {e}"}, status=502)

    o = Order.objects.create(
        user=request.user,
        package=package,
        razorpay_order_id=rp_order["id"],
        amount=amount_paise,
        currency="INR",
    )

    # (B) Create a Payment Link
    payment_link_url = None
    pl_meta = {}
    try:
        cust = {
            "name": (request.user.get_full_name() or request.user.username)[:100],
            "email": (getattr(request.user, "email", "") or None),
        }
        cust = {k: v for k, v in cust.items() if v}

        pl_req = {
            "amount": amount_paise,
            "currency": "INR",
            "reference_id": f"orderdb-{o.id}",
            "description": f"H2H: {package.name}",
            "customer": cust or None,
            "notify": {"email": True, "sms": False},
            "notes": {
                "package": package.name,
                "local_rp_order": rp_order["id"],
                "booking_id": str(booking_id or ""),
            },
        }
        if pl_req.get("customer") is None:
            pl_req.pop("customer")

        pl = client.payment_link.create(pl_req)
        payment_link_url = pl.get("short_url")
        if pl.get("order_id"):
            o.razorpay_order_id = pl["order_id"]
            o.save(update_fields=["razorpay_order_id"])
            rp_order["id"] = pl["order_id"]
        pl_meta = {"payment_link_id": pl.get("id")}
    except Exception as e:
        pl_meta = {"error": str(e)}

    # If we created order successfully and had a booking, link them
    if booking:
        booking.order = o
        booking.save(update_fields=["order"])

    return Response({
        "order": rp_order,
        "key_id": getattr(settings, "RAZORPAY_KEY_ID", None),
        "order_db": OrderSerializer(o).data,
        "payment_link": payment_link_url,
        "payment_link_meta": pl_meta,
        "pricing_snapshot": getattr(booking, "pricing_breakdown", None) if booking else None,
    })


# -----------------------------------
# Event-aware availability & allocation
# -----------------------------------
def _units_taken_for_event(event: Event):
    """
    Units already tied up for THIS event via allocations on pending/confirmed bookings.
    """
    taken = Allocation.objects.filter(
        unit=OuterRef("pk"),
        booking__event=event,
        booking__status__in=["PENDING_PAYMENT", "CONFIRMED"],
    )
    return Unit.objects.annotate(is_taken=Exists(taken)).filter(is_taken=True)



@transaction.atomic
def allocate_units_for_booking(booking: Booking, pkg: Package | None = None):
    """
    Allocate concrete Unit(s) so that:
      - Eligibility constraint = unit.unit_type in package.allowed_unit_types (ONLY).
      - No category check; property/category are chosen automatically.
      - booking.property, booking.unit_type, booking.category are AUTO-FILLED from the picked unit(s).
      - We try to satisfy capacity within a single (property, unit_type) cluster if possible.

    Preference order:
      1) If booking.unit_type is already set and is allowed -> prefer that unit_type.
      2) If booking.property is set -> prefer that property within the chosen unit_type.
      3) Otherwise, pick any cluster that can satisfy capacity.
    """
    if not booking.event:
        raise ValueError("Booking missing event")

    pkg = pkg or (getattr(booking, "order", None) and booking.order.package)
    if not pkg:
        raise ValueError("Package context missing")

    needed = max(1, int(booking.guests or 1))

    # --- resolve allowed unit types for the package (M2M -> fallback by name, case-insensitive) ---
    try:
        allowed_ids = list(pkg.allowed_unit_types.values_list("id", flat=True))
    except Exception:
        allowed_ids = []
    if not allowed_ids:
        names = _allowed_unit_types_for_package(pkg) or set()
        if names:
            ids = list(UnitType.objects.filter(name__in=names).values_list("id", flat=True))
            if not ids:
                # case-insensitive fallback
                ids = [ut.id for nm in names
                       for ut in [UnitType.objects.filter(name__iexact=nm).first()]
                       if ut]
            allowed_ids = ids
    if not allowed_ids:
        raise ValueError("Package has no allowed unit types configured")

    # If booking.unit_type is set and allowed, pin to it; otherwise ignore it.
    pinned_ut = booking.unit_type_id if (booking.unit_type_id in allowed_ids) else None

    taken_ids = set(_units_taken_for_event(booking.event).values_list("id", flat=True))
    qs = (Unit.objects
          .select_for_update()
          .filter(unit_type_id__in=( [pinned_ut] if pinned_ut else allowed_ids ),
                  status="AVAILABLE")
          .exclude(id__in=taken_ids))

    # (Optional) if booking.property is set, try that first by ordering
    # Order by: prefer same property (if any), then larger capacity to use fewer units
    if booking.property_id:
        qs = qs.order_by(
            # same property first
            models.Case(
                models.When(property_id=booking.property_id, then=models.Value(0)),
                default=models.Value(1),
                output_field=models.IntegerField(),
            ),
            "-capacity", "property__name", "unit_type__name", "label"
        )
    else:
        qs = qs.order_by("-capacity", "property__name", "unit_type__name", "label")

    pool = list(qs)
    if not pool:
        raise ValueError("Insufficient capacity for requested guests")

    # --- try to fit within a single (property, unit_type) cluster ---
    clusters = defaultdict(list)
    for u in pool:
        clusters[(u.property_id, u.unit_type_id)].append(u)

    # Cluster preference: if booking.property is set, prefer that; if pinned_ut, prefer that ut
    def cluster_score(key):
        prop_id, ut_id = key
        score = 0
        if booking.property_id and prop_id == booking.property_id:
            score -= 2
        if pinned_ut and ut_id == pinned_ut:
            score -= 1
        return score

    picks: list[Unit] = []
    for (prop_id, ut_id), units in sorted(clusters.items(), key=lambda kv: cluster_score(kv[0])):
        total = 0
        trial = []
        for u in units:
            trial.append(u)
            total += (u.capacity or 1)
            if total >= needed:
                picks = trial
                # force booking slice if not set (or if pinned_ut forced a unit_type)
                booking.property_id = prop_id
                booking.unit_type_id = ut_id
                break
        if picks:
            break

    # --- fallback: cross-cluster, minimize number of units ---
    if not picks:
        total = 0
        for u in pool:
            picks.append(u)
            total += (u.capacity or 1)
            if total >= needed:
                # set booking slice from first picked unit
                booking.property = picks[0].property
                booking.unit_type = picks[0].unit_type
                break

    if sum((u.capacity or 1) for u in picks) < needed:
        raise ValueError("Insufficient capacity for requested guests")

    # --- persist allocations, mark units, auto-fill category, status, dates ---
    for u in picks:
        Allocation.objects.create(booking=booking, unit=u)
        u.status = "OCCUPIED"
        u.save(update_fields=["status"])

    # keep category for the ticket (copied from chosen unit)
    booking.category = picks[0].category or (booking.category or "")
    booking.status = "CONFIRMED"
    if booking.event and (not booking.check_in or not booking.check_out):
        booking.check_in = booking.event.start_date
        booking.check_out = booking.event.end_date

    booking.save(update_fields=["property", "unit_type", "category", "status", "check_in", "check_out"])
    return picks


# -----------------------------------
# Inventory / Booking endpoints (EVENT-BASED)
# -----------------------------------
@api_view(["GET"])
@permission_classes([AllowAny])
def availability(request):
    """
    Query params (no category):
      OPTIONAL:
        - event_id=...                 (fallback: latest active & booking_open)
        - property_id=...              (filter to one property)
        - unit_type_ids=1,2            (CSV / '|' separated)
        - unit_type_codes=DT,ST        (CSV / '|' separated, case-insensitive)
        - unit_type_names=DOME TENT,...(CSV / '|' separated, case-insensitive)
        - package_id=...               (derive/validate allowed unit types)

    Behavior:
      - If ONLY package_id is provided, unit types are derived from package.allowed_unit_types.
      - If package_id AND unit types are provided, all provided unit types are validated
        against that package (error if any disallowed).
      - If neither package_id nor any unit types are provided => 400.
      - Returns total availability + per-unit-type breakdown within the event window,
        optionally limited to a property.
    """
    # ---- helpers ----
    def _csv_list(val):
        if not val:
            return []
        if isinstance(val, list):
            return [str(v).strip() for v in val if str(v).strip()]
        return [p.strip() for p in str(val).replace("|", ",").split(",") if p.strip()]

    def _fetch_utypes_by_ids(ids_csv):
        ids = []
        for x in _csv_list(ids_csv):
            try:
                ids.append(int(x))
            except Exception:
                pass
        return list(UnitType.objects.filter(id__in=ids)), [x for x in _csv_list(ids_csv) if not x.isdigit()]

    def _fetch_utypes_by_codes(codes_csv):
        found, missing = [], []
        for code in _csv_list(codes_csv):
            ut = UnitType.objects.filter(code__iexact=code).first()
            if ut:
                found.append(ut)
            else:
                missing.append(code)
        return found, missing

    def _fetch_utypes_by_names(names_csv):
        found, missing = [], []
        for name in _csv_list(names_csv):
            ut = UnitType.objects.filter(name__iexact=name).first()
            if ut:
                found.append(ut)
            else:
                missing.append(name)
        return found, missing

    # ---- event (fallback to current bookable) ----
    event_id = request.GET.get("event_id")
    if event_id:
        try:
            event = Event.objects.get(id=event_id, active=True)
        except Event.DoesNotExist:
            return Response({"error": "event not found or not active", "event_id": event_id}, status=400)
    else:
        event = (Event.objects.filter(active=True, booking_open=True)
                 .order_by("-year", "-start_date").first())
        if not event:
            return Response({"error": "no active/bookable event found; pass event_id explicitly"}, status=400)

    # ---- optional property ----
    prop = None
    property_id = request.GET.get("property_id")
    if property_id:
        try:
            prop = Property.objects.get(id=property_id)
        except Property.DoesNotExist:
            return Response({"error": "property not found", "property_id": property_id}, status=400)

    # ---- resolve requested unit types (ids / codes / names) ----
    requested_utypes = []
    missing_hint = {"ids": [], "codes": [], "names": []}

    if request.GET.get("unit_type_ids"):
        f, bad_ids = _fetch_utypes_by_ids(request.GET.get("unit_type_ids"))
        requested_utypes.extend(f)
        missing_hint["ids"].extend(bad_ids)

    if request.GET.get("unit_type_codes"):
        f, miss = _fetch_utypes_by_codes(request.GET.get("unit_type_codes"))
        requested_utypes.extend(f)
        missing_hint["codes"].extend(miss)

    if request.GET.get("unit_type_names"):
        f, miss = _fetch_utypes_by_names(request.GET.get("unit_type_names"))
        requested_utypes.extend(f)
        missing_hint["names"].extend(miss)

    # de-dup by id
    requested_utypes_map = {ut.id: ut for ut in requested_utypes}
    requested_utypes = list(requested_utypes_map.values())

    # ---- package: derive/validate unit types ----
    pkg = None
    pkg_id = request.GET.get("package_id")
    if pkg_id:
        try:
            pkg = Package.objects.get(id=pkg_id, active=True)
        except Package.DoesNotExist:
            return Response({"error": "invalid package"}, status=400)

        # derive allowed set for this package
        allowed_qs = pkg.allowed_unit_types.all()
        allowed_map = {ut.id: ut for ut in allowed_qs}
        if not allowed_map:
            # fallback to your static map if M2M empty
            # names like COTTAGE, HUT, SWISS TENT, DOME TENT
            allowed_names = _allowed_unit_types_for_package(pkg)
            if allowed_names:
                for nm in allowed_names:
                    ut = UnitType.objects.filter(name__iexact=nm).first()
                    if ut:
                        allowed_map[ut.id] = ut

        if requested_utypes:
            # validate provided unit types against allowed set
            disallowed = [ut for ut in requested_utypes if ut.id not in allowed_map]
            if disallowed:
                return Response({
                    "error": "unit_types not allowed for this package",
                    "package_id": pkg_id,
                    "disallowed": [ut.name for ut in disallowed],
                    "allowed_unit_types": [u.name for u in allowed_map.values()],
                }, status=400)
            # limit search to requested list (already validated)
            final_utypes = requested_utypes
        else:
            # no explicit unit types -> use package's allowed ones
            final_utypes = list(allowed_map.values())
            if not final_utypes:
                return Response({
                    "error": "package has no allowed unit types configured (and fallback found none)"
                }, status=400)
    else:
        # no package given: require at least one unit type
        if not requested_utypes:
            return Response({
                "error": "unit_types required (ids/codes/names) or supply package_id",
                "missing": {k: v for k, v in missing_hint.items() if v}
            }, status=400)
        final_utypes = requested_utypes

    # ---- availability within event ----
    taken_ids = _units_taken_for_event(event).values_list("id", flat=True)

    base_qs = Unit.objects.filter(unit_type__in=[ut.id for ut in final_utypes])
    if prop:
        base_qs = base_qs.filter(property=prop)

    free_qs = base_qs.exclude(id__in=taken_ids)

    # per-unit-type breakdown
    breakdown = []
    total_units = 0
    total_capacity = 0

    for ut in final_utypes:
        ut_qs = free_qs.filter(unit_type=ut)
        cnt = ut_qs.count()
        cap = 0
        for u in ut_qs.iterator(chunk_size=500):
            cap += (u.capacity or 1)
        total_units += cnt
        total_capacity += cap
        breakdown.append({
            "unit_type": UnitTypeSerializer(ut).data,
            "available_units": cnt,
            "total_capacity": cap,
        })

    return Response({
        "event": EventSerializer(event).data,
        "property": PropertySerializer(prop).data if prop else None,
        "package": PackageSerializer(pkg).data if pkg else None,
        "unit_types_requested": [ut.id for ut in requested_utypes] if requested_utypes else None,
        "available_units": total_units,
        "total_capacity": total_capacity,
        "breakdown": breakdown,
    })

def _get_live_promocode(code: str) -> PromoCode | None:
    if not code:
        return None
    promo = PromoCode.objects.filter(code__iexact=str(code).strip()).first()
    if not promo:
        return None
    return promo if promo.is_live_today() else None

def _apply_promocode(total_inr: int, promo: PromoCode | None) -> tuple[int, int, dict | None]:
    """
    Return (discount_inr, final_total_inr, promo_breakdown or None).
    Enforces final_total >= 1 INR.
    """
    if not promo:
        return 0, int(total_inr), None

    try:
        if promo.kind == "PERCENT":
            discount = int(total_inr * (promo.value / 100.0))
        else:
            discount = int(promo.value)
    except Exception:
        discount = 0

    discount = max(0, min(discount, max(0, total_inr - 1)))  # keep at least ₹1 payable
    final = int(total_inr) - discount

    breakdown = {
        "code": promo.code,
        "kind": promo.kind,
        "value": promo.value,
        "discount_inr": discount,
        "final_total_inr": final,
    }
    return discount, final, breakdown


# ---------- PUBLIC: Validate promocode (ADD) ----------
@api_view(["GET"])
@permission_classes([AllowAny])
def validate_promocode(request):
    """
    GET params:
      code=H2H10
      amount_inr=5000             # optional base to preview (if not provided, tries booking_id or package_id)
      booking_id=123              # optional; uses computed booking price
      package_id=1                # optional; falls back to package base price
    """
    code = (request.GET.get("code") or "").strip()
    if not code:
        return Response({"valid": False, "reason": "code_required"}, status=400)

    promo = _get_live_promocode(code)
    if not promo:
        return Response({"valid": False, "reason": "invalid_or_expired"}, status=200)

    # Determine a base amount to preview discount
    amount = request.GET.get("amount_inr")
    base_inr = None

    if amount is not None:
        try:
            base_inr = max(1, int(float(amount)))
        except Exception:
            return Response({"valid": False, "reason": "bad_amount"}, status=400)

    elif request.GET.get("booking_id"):
        try:
            booking = Booking.objects.get(id=int(request.GET["booking_id"]))
            # choose a package context if the booking is linked to an order else use any active package?
            # We'll use the order's package if present, else require package_id param.
            pkg = booking.order.package if booking.order_id else None
            if not pkg and request.GET.get("package_id"):
                pkg = Package.objects.get(id=int(request.GET["package_id"]), active=True)
            if not pkg:
                return Response({"valid": False, "reason": "package_required"}, status=400)
            # reuse your pricing rules
            base_inr, _, _ = _compute_booking_pricing(pkg, booking)
        except Exception:
            return Response({"valid": False, "reason": "bad_booking_or_package"}, status=400)

    elif request.GET.get("package_id"):
        try:
            pkg = Package.objects.get(id=int(request.GET["package_id"]), active=True)
            base_inr = int(pkg.price_inr)
        except Exception:
            return Response({"valid": False, "reason": "bad_package"}, status=400)

    else:
        return Response({"valid": True, "promo": {"code": promo.code, "kind": promo.kind, "value": promo.value}},
                        status=200)

    discount, final, br = _apply_promocode(base_inr, promo)
    return Response({"valid": True, "base_inr": base_inr, "discount_inr": discount, "final_inr": final, "promo": br})


# # ---------- create_booking: accept & store promo code (optional) ----------

def _sanitize_name(s: str) -> str:
    return (s or "").strip()[:100]

def _sanitize_bg(s: str) -> str:
    return (s or "").strip().upper()[:5]

def _sanitize_age(a):
    try:
        x = int(a)
        return max(0, min(120, x))
    except Exception:
        return None

def _normalize_companions(raw_list):
    """Return a clean list of companions (excluding primary)."""
    out = []
    if not isinstance(raw_list, list):
        return out
    for p in raw_list:
        if not isinstance(p, dict):
            continue
        name = _sanitize_name(p.get("name"))
        age = _sanitize_age(p.get("age"))
        bg = _sanitize_bg(p.get("blood_group"))
        if not name:
            continue
        out.append({"name": name, "age": age, "blood_group": bg})
    return out

def _extras_from_people(package: Package, total_people: int, ages_for_everyone: list[int]):
    """
    Compute chargeable extras beyond base_includes, choosing an allocation that
    applies base_includes to the most expensive people first (adults, then half, then free).
    Returns (extra_adults, extra_half, extra_free, extra_ages_list_only_for_extras)
    """
    base = int(package.base_includes or 1)
    free_max = int(package.child_free_max_age or 0)
    half_max = int(package.child_half_max_age or 0)

    # classify everyone
    adults = []
    halfs = []
    frees = []
    for age in ages_for_everyone:
        if age is None:
            adults.append(None)  # unknown age -> treat as adult
        elif age <= free_max:
            frees.append(age)
        elif age <= half_max:
            halfs.append(age)
        else:
            adults.append(age)

    # allocate base seats to the most expensive first (adults, then half, then free)
    remaining_base = max(0, base)
    def allocate(lst):
        nonlocal remaining_base
        take = min(len(lst), remaining_base)
        remaining_base -= take
        keep = lst[take:]  # those not covered by base -> become extras
        return keep

    adults_extra = allocate(adults)
    halfs_extra  = allocate(halfs)
    frees_extra  = allocate(frees)

    extra_adults = len(adults_extra)
    extra_half = len(halfs_extra)
    extra_free = len(frees_extra)

    # extra ages list (only extras) for snapshot
    extra_ages = []
    extra_ages.extend([a for a in adults_extra if a is not None])
    extra_ages.extend([a for a in halfs_extra if a is not None])
    extra_ages.extend([a for a in frees_extra if a is not None])

    return extra_adults, extra_half, extra_free, extra_ages


def _dbg(msg, **kw):
    """Compact JSON log; avoid duplicate console prints."""
    try:
        payload = json.dumps(kw, default=str)[:4000]
    except Exception:
        payload = str(kw)
    logger.warning("[create_booking] %s | %s", msg, payload)



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_booking(request):
    """
    Creates a booking WITHOUT requiring property_id or unit_type_id.
    We only need: event_id, package_id (or order_id), optional companions/category/etc.
    Concrete property/unit_type/units are auto-chosen after payment by the allocator.
    """
    # --- request surface ---
    _dbg(
        "ENTRY",
        method=request.method,
        path=getattr(request, "get_full_path", lambda: "")(),
        is_auth=getattr(request.user, "is_authenticated", False),
        user_id=getattr(request.user, "id", None),
        sessionid_present=bool(request.COOKIES.get("sessionid")),
        csrf_cookie=request.COOKIES.get("csrftoken"),
        csrf_header=request.META.get("HTTP_X_CSRFTOKEN"),
        content_type=request.META.get("CONTENT_TYPE"),
    )

    data = request.data
    _dbg("RAW_PAYLOAD_KEYS", keys=list(data.keys()))

    # ---- validate identifiers early with explicit errors ----
    try:
        event_id = int(data.get("event_id"))
    except Exception:
        _dbg("BAD_event_id", got=data.get("event_id"))
        return Response({"error": "invalid_event_id", "detail": "event_id must be an integer"}, status=400)

    # ---- model lookups (with precise error) ----
    try:
        event = Event.objects.get(id=event_id, active=True, booking_open=True)
    except Event.DoesNotExist:
        _dbg("EVENT_NOT_FOUND_OR_CLOSED", event_id=event_id)
        return Response({"error": "invalid_event", "event_id": event_id}, status=400)

    # ---- basic fields ----
    category = (data.get("category") or "").strip().upper()
    _dbg("CATEGORY_RESOLVED", category=category)

    # companions + guests normalization
    companions = _normalize_companions(data.get("companions") or [])
    guests_total = int(data.get("guests") or (1 + len(companions)))
    if guests_total != (1 + len(companions)):
        _dbg("GUESTS_ADJUSTED", before=guests_total, companions_len=len(companions))
        guests_total = 1 + len(companions)

    blood_group = _sanitize_bg(data.get("blood_group"))
    emer_name = _sanitize_name(data.get("emergency_contact_name"))
    emer_phone = (data.get("emergency_contact_phone") or "").strip()[:32]
    _dbg(
        "PRIMARY_INFO",
        blood_group=blood_group,
        emer_name=emer_name,
        emer_phone=emer_phone,
        guests_total=guests_total,
        companions_len=len(companions),
    )

    # ---- package / order selection ----
    package = None
    order = None
    order_id = data.get("order_id")
    package_id = data.get("package_id")

    if order_id:
        try:
            order = Order.objects.get(id=order_id, user=request.user)
            package = order.package
            _dbg("USING_ORDER_PACKAGE", order_id=order_id, package_id=getattr(package, "id", None))
        except Order.DoesNotExist:
            _dbg("BAD_ORDER_ID", order_id=order_id)
            return Response({"error": "invalid_order_id", "order_id": order_id}, status=400)
    else:
        if not package_id:
            _dbg("MISSING_PACKAGE_ID")
            return Response({"error": "package_id_required"}, status=400)
        try:
            package = Package.objects.get(id=package_id, active=True)
        except Package.DoesNotExist:
            _dbg("BAD_PACKAGE_ID", package_id=package_id)
            return Response({"error": "invalid_package_id", "package_id": package_id}, status=400)

    # ---- derive extras from ages (primary + companions) ----
    ages_everyone = [_sanitize_age(data.get("primary_age"))]  # may be None (treated as adult)
    ages_everyone.extend([c.get("age") for c in companions])
    try:
        extra_adults, extra_half, extra_free, extra_ages_only = _extras_from_people(
            package, guests_total, ages_everyone
        )
    except Exception as ex:
        _dbg("EXTRAS_COMPUTE_FAILED", err=str(ex), ages=ages_everyone, guests_total=guests_total)
        return Response({"error": "extras_compute_failed", "detail": str(ex)}, status=400)
    _dbg(
        "EXTRAS_COMPUTED",
        extra_adults=extra_adults,
        extra_half=extra_half,
        extra_free=extra_free,
        ages_everyone=ages_everyone,
    )

    # ---- promo (optional) ----
    promo = None
    if data.get("promo_code"):
        code_raw = str(data["promo_code"])
        promo = _get_live_promocode(code_raw)
        if not promo:
            _dbg("INVALID_PROMO", promo_code=code_raw)
            return Response({"error": "invalid_or_expired_promocode"}, status=400)
        _dbg("PROMO_OK", code=getattr(promo, "code", None))

    # ---- create booking (NO property/unit_type at this stage) ----
    try:
        booking = Booking.objects.create(
            user=request.user,
            event=event,
            property=None,               # auto-pick later
            unit_type=None,              # auto-pick later
            category=category,
            guests=max(1, guests_total),
            companions=companions,
            status="PENDING_PAYMENT",
            order=order if order else None,
            check_in=event.start_date,
            check_out=event.end_date,
            blood_group=blood_group,
            emergency_contact_name=emer_name,
            emergency_contact_phone=emer_phone,
            guest_ages=extra_ages_only,  # extras-only ages snapshot
            extra_adults=extra_adults,
            extra_children_half=extra_half,
            extra_children_free=extra_free,
            promo_code=promo,
        )
    except Exception as ex:
        _dbg("BOOKING_CREATE_FAILED", err=str(ex))
        return Response({"error": "booking_create_failed", "detail": str(ex)}, status=400)

    _dbg("BOOKING_CREATED", booking_id=booking.id)
    return Response(BookingSerializer(booking).data, status=201)




# @api_view(["POST"])
# @permission_classes([IsAuthenticated])
# def create_order(request):
#     """
#     Body:
#     {
#       "package_id": 1,
#       "booking_id": 123,
#       "promo_code": "H2H10",
#       "debug": true
#     }

#     Capacity precheck ignores Booking.category. Only unit_type (from Package) matters.
#     """
#     debug_mode = str(request.data.get("debug") or "").lower() in ("1", "true", "yes", "y")
#     package_id = request.data.get("package_id")
#     booking_id = request.data.get("booking_id")
#     req_promo_code = (request.data.get("promo_code") or "").strip() or None

#     _dbg("CREATE_ORDER:ENTRY",
#          path=getattr(request, "get_full_path", lambda: "")(),
#          user_id=getattr(request.user, "id", None),
#          package_id=package_id,
#          booking_id=booking_id,
#          req_promo_code=req_promo_code,
#          debug_mode=debug_mode)

#     if not package_id:
#         _dbg("CREATE_ORDER:ERR_NO_PACKAGE_ID")
#         return Response({"error": "package_id required"}, status=400)

#     try:
#         package = Package.objects.get(id=package_id, active=True)
#         _dbg("CREATE_ORDER:PACKAGE_OK", package_id=package.id, package_name=package.name)
#     except Package.DoesNotExist:
#         _dbg("CREATE_ORDER:ERR_BAD_PACKAGE", package_id=package_id)
#         return Response({"error": "invalid package"}, status=404)

#     total_inr = int(package.price_inr)
#     booking = None
#     breakdown = None
#     guests_total = None

#     if booking_id:
#         try:
#             booking = Booking.objects.get(id=booking_id, user=request.user)
#             _dbg("CREATE_ORDER:BOOKING_OK",
#                  booking_id=booking.id,
#                  event_id=booking.event_id,
#                  guests=booking.guests,
#                  category=(booking.category or "").strip().upper())
#         except Booking.DoesNotExist:
#             _dbg("CREATE_ORDER:ERR_BAD_BOOKING", booking_id=booking_id)
#             return Response({"error": "invalid booking_id"}, status=400)

#         try:
#             total_inr, breakdown, guests_total = _compute_booking_pricing(package, booking)
#             _dbg("CREATE_ORDER:PRICING_OK",
#                  total_inr=total_inr,
#                  guests_total=guests_total,
#                  computed_from=(breakdown or {}).get("computed_from"))
#         except Exception as ex:
#             _dbg("CREATE_ORDER:PRICING_ERR", err=str(ex))
#             return Response({"error": "pricing_failed", "detail": str(ex)}, status=400)

#     # ---- capacity precheck (ignore category) ----
#     if booking and booking.event_id:
#         def _allowed_utype_ids_for_package(pkg: Package) -> list[int]:
#             try:
#                 m2m_ids = list(pkg.allowed_unit_types.values_list("id", flat=True))
#             except Exception:
#                 m2m_ids = []
#             if m2m_ids:
#                 return m2m_ids
#             names = _allowed_unit_types_for_package(pkg) or set()
#             if not names:
#                 return []
#             ids = list(UnitType.objects.filter(name__in=names).values_list("id", flat=True))
#             if not ids:
#                 ids = [ut.id for nm in names
#                        for ut in [UnitType.objects.filter(name__iexact=nm).first()]
#                        if ut]
#             return ids

#         allowed_ids = _allowed_utype_ids_for_package(package)
#         _dbg("CREATE_ORDER:ALLOWED_UNIT_TYPES",
#              source="M2M" if list(package.allowed_unit_types.all()) else "FALLBACK",
#              allowed_ids=allowed_ids,
#              allowed_names=list(UnitType.objects.filter(id__in=allowed_ids).values_list("name", flat=True)))

#         if not allowed_ids:
#             return Response({
#                 "error": "package_has_no_allowed_unit_types",
#                 "message": "Configure allowed unit types on the package or ensure fallback map matches UnitType names."
#             }, status=400)

#         taken_ids = set(_units_taken_for_event(booking.event).values_list("id", flat=True))

#         # IGNORE category here:
#         qs_free = (Unit.objects
#                    .filter(unit_type_id__in=allowed_ids, status="AVAILABLE")
#                    .exclude(id__in=taken_ids)
#                    .order_by("-capacity"))

#         pool_count = qs_free.count()
#         needed = max(1, int(booking.guests or 1))
#         cap = 0
#         sample = []
#         for u in qs_free.iterator(chunk_size=500):
#             if len(sample) < 10:
#                 sample.append({
#                     "id": u.id,
#                     "label": u.label,
#                     "property": getattr(u.property, "name", ""),
#                     "unit_type": getattr(u.unit_type, "name", ""),
#                     "category": u.category,
#                     "capacity": u.capacity or 1,
#                 })
#             cap += (u.capacity or 1)
#             if cap >= needed:
#                 break

#         _dbg("CREATE_ORDER:CAPACITY_CHECK_IGNORE_CATEGORY",
#              needed=needed, cap_seen=cap, pool_count=pool_count,
#              taken_ids_count=len(taken_ids), sample_units=sample)

#         if cap < needed:
#             return Response({
#                 "error": "house_full",
#                 "message": "No capacity left for this package for the event (category ignored).",
#                 "debug": {
#                     "event_id": booking.event_id,
#                     "package_id": package.id,
#                     "needed": needed,
#                     "allowed_unit_type_names": list(
#                         UnitType.objects.filter(id__in=allowed_ids).values_list("name", flat=True)
#                     ),
#                     "pool_units": pool_count,
#                     "capacity_available": cap,
#                     "units_sample": sample
#                 }
#             }, status=409)

#     # ---- promo ----
#     promo = booking.promo_code if (booking and booking.promo_code_id) else _get_live_promocode(req_promo_code)
#     _dbg("CREATE_ORDER:PROMO",
#          source="booking" if (booking and booking.promo_code_id) else "request",
#          code=(getattr(promo, "code", None) or None))

#     try:
#         promo_discount, final_inr, promo_br = _apply_promocode(total_inr, promo)
#         _dbg("CREATE_ORDER:PROMO_APPLIED",
#              total_before=total_inr, discount=promo_discount, total_after=final_inr)
#     except Exception as ex:
#         _dbg("CREATE_ORDER:PROMO_ERR", err=str(ex))
#         return Response({"error": "promo_apply_failed", "detail": str(ex)}, status=400)

#     if booking:
#         try:
#             if promo_br:
#                 breakdown = breakdown or {}
#                 breakdown = {**breakdown, "promo": promo_br}
#             booking.pricing_total_inr = final_inr
#             booking.pricing_breakdown = breakdown or booking.pricing_breakdown
#             booking.guests = guests_total if guests_total is not None else booking.guests
#             booking.promo_discount_inr = promo_discount
#             booking.promo_breakdown = promo_br
#             if promo and not booking.promo_code_id:
#                 booking.promo_code = promo
#             booking.save(update_fields=[
#                 "pricing_total_inr", "pricing_breakdown", "guests",
#                 "promo_discount_inr", "promo_breakdown", "promo_code"
#             ])
#             _dbg("CREATE_ORDER:BOOKING_SNAPSHOT_OK", booking_id=booking.id, final_inr=final_inr)
#         except Exception as ex:
#             _dbg("CREATE_ORDER:BOOKING_SNAPSHOT_ERR", err=str(ex))
#             return Response({"error": "pricing_snapshot_failed", "detail": str(ex)}, status=400)

#     amount_paise = max(1, int(final_inr)) * 100
#     try:
#         client = _get_razorpay_client()
#     except RuntimeError as e:
#         _dbg("CREATE_ORDER:RAZORPAY_CLIENT_ERR", err=str(e))
#         return Response({"error": str(e)}, status=503)

#     try:
#         rp_order = client.order.create({
#             "amount": amount_paise,
#             "currency": "INR",
#             "payment_capture": 1,
#             "notes": {
#                 "package": package.name,
#                 "booking_id": booking_id or "",
#                 "promo_code": (promo and promo.code) or ""
#             },
#         })
#         _dbg("CREATE_ORDER:RP_ORDER_OK", rp_order_id=rp_order.get("id"), amount_paise=amount_paise)
#     except Exception as e:
#         _dbg("CREATE_ORDER:RP_ORDER_ERR", err=str(e))
#         return Response({"error": f"Failed to create Razorpay order: {e}"}, status=502)

#     try:
#         o = Order.objects.create(
#             user=request.user,
#             package=package,
#             razorpay_order_id=rp_order["id"],
#             amount=amount_paise,
#             currency="INR",
#         )
#         _dbg("CREATE_ORDER:ORDER_DB_OK", order_db_id=o.id, rp_order_id=o.razorpay_order_id)
#     except Exception as e:
#         _dbg("CREATE_ORDER:ORDER_DB_ERR", err=str(e))
#         return Response({"error": "order_db_create_failed", "detail": str(e)}, status=500)

#     payment_link_url = None
#     pl_meta = {}
#     try:
#         cust = {
#             "name": (request.user.get_full_name() or request.user.username)[:100],
#             "email": (getattr(request.user, "email", "") or None),
#         }
#         cust = {k: v for k, v in cust.items() if v}
#         pl_req = {
#             "amount": amount_paise,
#             "currency": "INR",
#             "reference_id": f"orderdb-{o.id}",
#             "description": f"H2H: {package.name}",
#             "customer": cust or None,
#             "notify": {"email": True, "sms": False},
#             "notes": {
#                 "package": package.name,
#                 "local_rp_order": rp_order["id"],
#                 "booking_id": str(booking_id or ""),
#                 "promo_code": (promo and promo.code) or "",
#                 "promo_discount_inr": promo_discount,
#             },
#         }
#         if pl_req.get("customer") is None:
#             pl_req.pop("customer")
#         pl = client.payment_link.create(pl_req)
#         payment_link_url = pl.get("short_url")
#         if pl.get("order_id"):
#             o.razorpay_order_id = pl["order_id"]
#             o.save(update_fields=["razorpay_order_id"])
#             rp_order["id"] = pl["order_id"]
#         pl_meta = {"payment_link_id": pl.get("id")}
#         _dbg("CREATE_ORDER:RP_PAYMENT_LINK_OK",
#              payment_link_id=pl_meta.get("payment_link_id"),
#              payment_link_url=payment_link_url)
#     except Exception as e:
#         pl_meta = {"error": str(e)}
#         _dbg("CREATE_ORDER:RP_PAYMENT_LINK_ERR", err=str(e))

#     if booking:
#         try:
#             booking.order = o
#             booking.save(update_fields=["order"])
#             _dbg("CREATE_ORDER:BOOKING_LINKED_TO_ORDER", booking_id=booking.id, order_db_id=o.id)
#         except Exception as e:
#             _dbg("CREATE_ORDER:BOOKING_LINK_ERR", err=str(e))

#     return Response({
#         "order": rp_order,
#         "key_id": getattr(settings, "RAZORPAY_KEY_ID", None),
#         "order_db": OrderSerializer(o).data,
#         "payment_link": payment_link_url,
#         "payment_link_meta": pl_meta,
#         "pricing_snapshot": getattr(booking, "pricing_breakdown", None) if booking else {
#             "base": {"includes": package.base_includes, "price_inr": int(package.price_inr)},
#             "promo": promo_br,
#             "total_inr_before_promo": total_inr,
#             "total_inr": final_inr,
#         },
#     })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_order(request):
    """
    Body:
    {
      "package_id": 1,
      "booking_id": 123,
      "promo_code": "H2H10",
      "debug": true
    }

    Capacity precheck ignores Booking.category. Only unit_type (from Package) matters.
    """
    debug_mode = str(request.data.get("debug") or "").lower() in ("1", "true", "yes", "y")
    package_id = request.data.get("package_id")
    booking_id = request.data.get("booking_id")
    req_promo_code = (request.data.get("promo_code") or "").strip() or None

    _dbg("CREATE_ORDER:ENTRY",
         path=getattr(request, "get_full_path", lambda: "")(),
         user_id=getattr(request.user, "id", None),
         package_id=package_id,
         booking_id=booking_id,
         req_promo_code=req_promo_code,
         debug_mode=debug_mode)

    if not package_id:
        _dbg("CREATE_ORDER:ERR_NO_PACKAGE_ID")
        return Response({"error": "package_id required"}, status=400)

    try:
        package = Package.objects.get(id=package_id, active=True)
        _dbg("CREATE_ORDER:PACKAGE_OK", package_id=package.id, package_name=package.name)
    except Package.DoesNotExist:
        _dbg("CREATE_ORDER:ERR_BAD_PACKAGE", package_id=package_id)
        return Response({"error": "invalid package"}, status=404)

    total_inr = int(package.price_inr)
    booking = None
    breakdown = None
    guests_total = None

    if booking_id:
        try:
            booking = Booking.objects.get(id=booking_id, user=request.user)
            _dbg("CREATE_ORDER:BOOKING_OK",
                 booking_id=booking.id,
                 event_id=booking.event_id,
                 guests=booking.guests,
                 category=(booking.category or "").strip().upper())
        except Booking.DoesNotExist:
            _dbg("CREATE_ORDER:ERR_BAD_BOOKING", booking_id=booking_id)
            return Response({"error": "invalid booking_id"}, status=400)

        try:
            total_inr, breakdown, guests_total = _compute_booking_pricing(package, booking)
            _dbg("CREATE_ORDER:PRICING_OK",
                 total_inr=total_inr,
                 guests_total=guests_total,
                 computed_from=(breakdown or {}).get("computed_from"))
        except Exception as ex:
            _dbg("CREATE_ORDER:PRICING_ERR", err=str(ex))
            return Response({"error": "pricing_failed", "detail": str(ex)}, status=400)

    # ---- capacity precheck (ignore category) ----
    if booking and booking.event_id:
        def _allowed_utype_ids_for_package(pkg: Package) -> list[int]:
            try:
                m2m_ids = list(pkg.allowed_unit_types.values_list("id", flat=True))
            except Exception:
                m2m_ids = []
            if m2m_ids:
                return m2m_ids
            names = _allowed_unit_types_for_package(pkg) or set()
            if not names:
                return []
            ids = list(UnitType.objects.filter(name__in=names).values_list("id", flat=True))
            if not ids:
                ids = [ut.id for nm in names
                       for ut in [UnitType.objects.filter(name__iexact=nm).first()]
                       if ut]
            return ids

        allowed_ids = _allowed_utype_ids_for_package(package)
        _dbg("CREATE_ORDER:ALLOWED_UNIT_TYPES",
             source="M2M" if list(package.allowed_unit_types.all()) else "FALLBACK",
             allowed_ids=allowed_ids,
             allowed_names=list(UnitType.objects.filter(id__in=allowed_ids).values_list("name", flat=True)))

        if not allowed_ids:
            return Response({
                "error": "package_has_no_allowed_unit_types",
                "message": "Configure allowed unit types on the package or ensure fallback map matches UnitType names."
            }, status=400)

        taken_ids = set(_units_taken_for_event(booking.event).values_list("id", flat=True))

        qs_free = (Unit.objects
                   .filter(unit_type_id__in=allowed_ids, status="AVAILABLE")
                   .exclude(id__in=taken_ids)
                   .order_by("-capacity"))

        pool_count = qs_free.count()
        needed = max(1, int(booking.guests or 1))
        cap = 0
        sample = []
        for u in qs_free.iterator(chunk_size=500):
            if len(sample) < 10:
                sample.append({
                    "id": u.id,
                    "label": u.label,
                    "property": getattr(u.property, "name", ""),
                    "unit_type": getattr(u.unit_type, "name", ""),
                    "category": u.category,
                    "capacity": u.capacity or 1,
                })
            cap += (u.capacity or 1)
            if cap >= needed:
                break

        _dbg("CREATE_ORDER:CAPACITY_CHECK_IGNORE_CATEGORY",
             needed=needed, cap_seen=cap, pool_count=pool_count,
             taken_ids_count=len(taken_ids), sample_units=sample)

        if cap < needed:
            return Response({
                "error": "house_full",
                "message": "No capacity left for this package for the event (category ignored).",
                "debug": {
                    "event_id": booking.event_id,
                    "package_id": package.id,
                    "needed": needed,
                    "allowed_unit_type_names": list(
                        UnitType.objects.filter(id__in=allowed_ids).values_list("name", flat=True)
                    ),
                    "pool_units": pool_count,
                    "capacity_available": cap,
                    "units_sample": sample
                }
            }, status=409)

    # ---- promo ----
    promo = booking.promo_code if (booking and booking.promo_code_id) else _get_live_promocode(req_promo_code)
    _dbg("CREATE_ORDER:PROMO",
         source="booking" if (booking and booking.promo_code_id) else "request",
         code=(getattr(promo, "code", None) or None))

    try:
        promo_discount, final_inr, promo_br = _apply_promocode(total_inr, promo)
        _dbg("CREATE_ORDER:PROMO_APPLIED",
             total_before=total_inr, discount=promo_discount, total_after=final_inr)
    except Exception as ex:
        _dbg("CREATE_ORDER:PROMO_ERR", err=str(ex))
        return Response({"error": "promo_apply_failed", "detail": str(ex)}, status=400)

    if booking:
        try:
            if promo_br:
                breakdown = breakdown or {}
                breakdown = {**breakdown, "promo": promo_br}
            booking.pricing_total_inr = final_inr
            booking.pricing_breakdown = breakdown or booking.pricing_breakdown
            booking.guests = guests_total if guests_total is not None else booking.guests
            booking.promo_discount_inr = promo_discount
            booking.promo_breakdown = promo_br
            if promo and not booking.promo_code_id:
                booking.promo_code = promo
            booking.save(update_fields=[
                "pricing_total_inr", "pricing_breakdown", "guests",
                "promo_discount_inr", "promo_breakdown", "promo_code"
            ])
            _dbg("CREATE_ORDER:BOOKING_SNAPSHOT_OK", booking_id=booking.id, final_inr=final_inr)
        except Exception as ex:
            _dbg("CREATE_ORDER:BOOKING_SNAPSHOT_ERR", err=str(ex))
            return Response({"error": "pricing_snapshot_failed", "detail": str(ex)}, status=400)

    amount_paise = max(1, int(final_inr)) * 100
    try:
        client = _get_razorpay_client()
    except RuntimeError as e:
        _dbg("CREATE_ORDER:RAZORPAY_CLIENT_ERR", err=str(e))
        return Response({"error": str(e)}, status=503)

    # ---- Razorpay ORDER (keep this so you can use its id for ticketing) ----
    try:
        rp_order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "payment_capture": 1,
            "notes": {
                "package": package.name,
                "booking_id": booking_id or "",
                "promo_code": (promo and promo.code) or ""
            },
        })
        _dbg("CREATE_ORDER:RP_ORDER_OK", rp_order_id=rp_order.get("id"), amount_paise=amount_paise)
    except Exception as e:
        _dbg("CREATE_ORDER:RP_ORDER_ERR", err=str(e))
        return Response({"error": f"Failed to create Razorpay order: {e}"}, status=502)

    # ---- Local ORDER row ----
    try:
        o = Order.objects.create(
            user=request.user,
            package=package,
            razorpay_order_id=rp_order["id"],
            amount=amount_paise,
            currency="INR",
        )
        _dbg("CREATE_ORDER:ORDER_DB_OK", order_db_id=o.id, rp_order_id=o.razorpay_order_id)
    except Exception as e:
        _dbg("CREATE_ORDER:ORDER_DB_ERR", err=str(e))
        return Response({"error": "order_db_create_failed", "detail": str(e)}, status=500)

    # ---- Payment Link (STANDARD) — MUST send amount & currency. DO NOT send order_id. ----
    payment_link_url = None
    pl_meta = {}
    try:
        cust = {
            "name": (request.user.get_full_name() or request.user.username)[:100],
            "email": (getattr(request.user, "email", "") or None),
        }
        cust = {k: v for k, v in cust.items() if v}

        pl_req = {
            "amount": amount_paise,
            "currency": "INR",
            "reference_id": f"orderdb-{o.id}",
            "description": f"H2H: {package.name}",
            "notify": {"email": True, "sms": False},
            "notes": {
                "package": package.name,
                "local_rp_order": rp_order["id"],  # for webhook fallback correlation
                "booking_id": str(booking_id or ""),
                "promo_code": (promo and promo.code) or "",
                "promo_discount_inr": promo_discount,
            },
        }
        if cust:
            pl_req["customer"] = cust

        _dbg("CREATE_ORDER:PL_REQ", sending={k: v for k, v in pl_req.items() if k != "customer"})
        pl = client.payment_link.create(pl_req)
        payment_link_url = pl.get("short_url") or pl.get("url")
        pl_meta = {"payment_link_id": pl.get("id")}
        _dbg("CREATE_ORDER:RP_PAYMENT_LINK_OK",
             payment_link_id=pl_meta.get("payment_link_id"),
             payment_link_url=payment_link_url)
    except Exception as e:
        pl_meta = {"error": str(e)}
        _dbg("CREATE_ORDER:RP_PAYMENT_LINK_ERR", err=str(e))

    if booking:
        try:
            booking.order = o
            booking.save(update_fields=["order"])
            _dbg("CREATE_ORDER:BOOKING_LINKED_TO_ORDER", booking_id=booking.id, order_db_id=o.id)
        except Exception as e:
            _dbg("CREATE_ORDER:BOOKING_LINK_ERR", err=str(e))

    return Response({
        "order": {"id": rp_order["id"], "amount": amount_paise, "currency": "INR"},
        "key_id": getattr(settings, "RAZORPAY_KEY_ID", None),
        "order_db": OrderSerializer(o).data,
        "payment_link": payment_link_url,          # <—— your frontend reads this
        "payment_link_meta": pl_meta,              # includes .error if creation failed
        "ticket_order_id": rp_order["id"],         # <—— used by /tickets/<id>.pdf
        "ticket_api_path": f"/api/tickets/{rp_order['id']}.pdf",
        "pricing_snapshot": getattr(booking, "pricing_breakdown", None) if booking else {
            "base": {"includes": package.base_includes, "price_inr": int(package.price_inr)},
            "promo": promo_br,
            "total_inr_before_promo": total_inr,
            "total_inr": final_inr,
        },
    })



# @api_view(["POST"])
# @permission_classes([IsAuthenticated])
# def create_order(request):
#     """
#     Body:
#     {
#       "package_id": 1,
#       "booking_id": 123,
#       "promo_code": "H2H10",
#       "debug": true
#     }

#     Capacity precheck ignores Booking.category. Only unit_type (from Package) matters.
#     """
#     debug_mode = str(request.data.get("debug") or "").lower() in ("1", "true", "yes", "y")
#     package_id = request.data.get("package_id")
#     booking_id = request.data.get("booking_id")
#     req_promo_code = (request.data.get("promo_code") or "").strip() or None

#     _dbg("CREATE_ORDER:ENTRY",
#          path=getattr(request, "get_full_path", lambda: "")(),
#          user_id=getattr(request.user, "id", None),
#          package_id=package_id,
#          booking_id=booking_id,
#          req_promo_code=req_promo_code,
#          debug_mode=debug_mode)

#     if not package_id:
#         _dbg("CREATE_ORDER:ERR_NO_PACKAGE_ID")
#         return Response({"error": "package_id required"}, status=400)

#     try:
#         package = Package.objects.get(id=package_id, active=True)
#         _dbg("CREATE_ORDER:PACKAGE_OK", package_id=package.id, package_name=package.name)
#     except Package.DoesNotExist:
#         _dbg("CREATE_ORDER:ERR_BAD_PACKAGE", package_id=package_id)
#         return Response({"error": "invalid package"}, status=404)

#     total_inr = int(package.price_inr)
#     booking = None
#     breakdown = None
#     guests_total = None

#     if booking_id:
#         try:
#             booking = Booking.objects.get(id=booking_id, user=request.user)
#             _dbg("CREATE_ORDER:BOOKING_OK",
#                  booking_id=booking.id,
#                  event_id=booking.event_id,
#                  guests=booking.guests,
#                  category=(booking.category or "").strip().upper())
#         except Booking.DoesNotExist:
#             _dbg("CREATE_ORDER:ERR_BAD_BOOKING", booking_id=booking_id)
#             return Response({"error": "invalid booking_id"}, status=400)

#         try:
#             total_inr, breakdown, guests_total = _compute_booking_pricing(package, booking)
#             _dbg("CREATE_ORDER:PRICING_OK",
#                  total_inr=total_inr,
#                  guests_total=guests_total,
#                  computed_from=(breakdown or {}).get("computed_from"))
#         except Exception as ex:
#             _dbg("CREATE_ORDER:PRICING_ERR", err=str(ex))
#             return Response({"error": "pricing_failed", "detail": str(ex)}, status=400)

#     # ---- capacity precheck (ignore category) ----
#     if booking and booking.event_id:
#         def _allowed_utype_ids_for_package(pkg: Package) -> list[int]:
#             try:
#                 m2m_ids = list(pkg.allowed_unit_types.values_list("id", flat=True))
#             except Exception:
#                 m2m_ids = []
#             if m2m_ids:
#                 return m2m_ids
#             names = _allowed_unit_types_for_package(pkg) or set()
#             if not names:
#                 return []
#             ids = list(UnitType.objects.filter(name__in=names).values_list("id", flat=True))
#             if not ids:
#                 ids = [ut.id for nm in names
#                        for ut in [UnitType.objects.filter(name__iexact=nm).first()]
#                        if ut]
#             return ids

#         allowed_ids = _allowed_utype_ids_for_package(package)
#         _dbg("CREATE_ORDER:ALLOWED_UNIT_TYPES",
#              source="M2M" if list(package.allowed_unit_types.all()) else "FALLBACK",
#              allowed_ids=allowed_ids,
#              allowed_names=list(UnitType.objects.filter(id__in=allowed_ids).values_list("name", flat=True)))

#         if not allowed_ids:
#             return Response({
#                 "error": "package_has_no_allowed_unit_types",
#                 "message": "Configure allowed unit types on the package or ensure fallback map matches UnitType names."
#             }, status=400)

#         taken_ids = set(_units_taken_for_event(booking.event).values_list("id", flat=True))

#         qs_free = (Unit.objects
#                    .filter(unit_type_id__in=allowed_ids, status="AVAILABLE")
#                    .exclude(id__in=taken_ids)
#                    .order_by("-capacity"))

#         pool_count = qs_free.count()
#         needed = max(1, int(booking.guests or 1))
#         cap = 0
#         sample = []
#         for u in qs_free.iterator(chunk_size=500):
#             if len(sample) < 10:
#                 sample.append({
#                     "id": u.id,
#                     "label": u.label,
#                     "property": getattr(u.property, "name", ""),
#                     "unit_type": getattr(u.unit_type, "name", ""),
#                     "category": u.category,
#                     "capacity": u.capacity or 1,
#                 })
#             cap += (u.capacity or 1)
#             if cap >= needed:
#                 break

#         _dbg("CREATE_ORDER:CAPACITY_CHECK_IGNORE_CATEGORY",
#              needed=needed, cap_seen=cap, pool_count=pool_count,
#              taken_ids_count=len(taken_ids), sample_units=sample)

#         if cap < needed:
#             return Response({
#                 "error": "house_full",
#                 "message": "No capacity left for this package for the event (category ignored).",
#                 "debug": {
#                     "event_id": booking.event_id,
#                     "package_id": package.id,
#                     "needed": needed,
#                     "allowed_unit_type_names": list(
#                         UnitType.objects.filter(id__in=allowed_ids).values_list("name", flat=True)
#                     ),
#                     "pool_units": pool_count,
#                     "capacity_available": cap,
#                     "units_sample": sample
#                 }
#             }, status=409)

#     # ---- promo ----
#     promo = booking.promo_code if (booking and booking.promo_code_id) else _get_live_promocode(req_promo_code)
#     _dbg("CREATE_ORDER:PROMO",
#          source="booking" if (booking and booking.promo_code_id) else "request",
#          code=(getattr(promo, "code", None) or None))

#     try:
#         promo_discount, final_inr, promo_br = _apply_promocode(total_inr, promo)
#         _dbg("CREATE_ORDER:PROMO_APPLIED",
#              total_before=total_inr, discount=promo_discount, total_after=final_inr)
#     except Exception as ex:
#         _dbg("CREATE_ORDER:PROMO_ERR", err=str(ex))
#         return Response({"error": "promo_apply_failed", "detail": str(ex)}, status=400)

#     if booking:
#         try:
#             if promo_br:
#                 breakdown = breakdown or {}
#                 breakdown = {**breakdown, "promo": promo_br}
#             booking.pricing_total_inr = final_inr
#             booking.pricing_breakdown = breakdown or booking.pricing_breakdown
#             booking.guests = guests_total if guests_total is not None else booking.guests
#             booking.promo_discount_inr = promo_discount
#             booking.promo_breakdown = promo_br
#             if promo and not booking.promo_code_id:
#                 booking.promo_code = promo
#             booking.save(update_fields=[
#                 "pricing_total_inr", "pricing_breakdown", "guests",
#                 "promo_discount_inr", "promo_breakdown", "promo_code"
#             ])
#             _dbg("CREATE_ORDER:BOOKING_SNAPSHOT_OK", booking_id=booking.id, final_inr=final_inr)
#         except Exception as ex:
#             _dbg("CREATE_ORDER:BOOKING_SNAPSHOT_ERR", err=str(ex))
#             return Response({"error": "pricing_snapshot_failed", "detail": str(ex)}, status=400)

#     amount_paise = max(1, int(final_inr)) * 100
#     try:
#         client = _get_razorpay_client()
#     except RuntimeError as e:
#         _dbg("CREATE_ORDER:RAZORPAY_CLIENT_ERR", err=str(e))
#         return Response({"error": str(e)}, status=503)

#     # ---- Razorpay order ----
#     try:
#         rp_order = client.order.create({
#             "amount": amount_paise,
#             "currency": "INR",
#             "payment_capture": 1,
#             "notes": {
#                 "package": package.name,
#                 "booking_id": booking_id or "",
#                 "promo_code": (promo and promo.code) or ""
#             },
#         })
#         _dbg("CREATE_ORDER:RP_ORDER_OK", rp_order_id=rp_order.get("id"), amount_paise=amount_paise)
#     except Exception as e:
#         _dbg("CREATE_ORDER:RP_ORDER_ERR", err=str(e))
#         return Response({"error": f"Failed to create Razorpay order: {e}"}, status=502)

#     # ---- local order ----
#     try:
#         o = Order.objects.create(
#             user=request.user,
#             package=package,
#             razorpay_order_id=rp_order["id"],
#             amount=amount_paise,
#             currency="INR",
#         )
#         _dbg("CREATE_ORDER:ORDER_DB_OK", order_db_id=o.id, rp_order_id=o.razorpay_order_id)
#     except Exception as e:
#         _dbg("CREATE_ORDER:ORDER_DB_ERR", err=str(e))
#         return Response({"error": "order_db_create_failed", "detail": str(e)}, status=500)

#     # ---- Payment Link (BOUND to SAME order_id; NO amount/currency here) ----
#     payment_link_url = None
#     pl_meta = {}
#     try:
#         cust = {
#             "name": (request.user.get_full_name() or request.user.username)[:100],
#             "email": (getattr(request.user, "email", "") or None),
#         }
#         cust = {k: v for k, v in cust.items() if v}

#         pl_req = {
#             "reference_id": f"orderdb-{o.id}",
#             "description": f"H2H: {package.name}",
#             "notify": {"email": True, "sms": False},
#             "notes": {
#                 "package": package.name,
#                 "local_rp_order": rp_order["id"],
#                 "booking_id": str(booking_id or ""),
#                 "promo_code": (promo and promo.code) or "",
#                 "promo_discount_inr": promo_discount,
#             },
#             "order_id": rp_order["id"],  # ******** key line: bind PL to the same order ********
#         }
#         if cust:
#             pl_req["customer"] = cust

#         _dbg("CREATE_ORDER:PL_REQ", sending={k: v for k, v in pl_req.items() if k != "customer"})
#         pl = client.payment_link.create(pl_req)
#         payment_link_url = pl.get("short_url") or pl.get("url")
#         pl_meta = {"payment_link_id": pl.get("id"), "order_id": pl.get("order_id")}
#         _dbg("CREATE_ORDER:RP_PAYMENT_LINK_OK",
#              payment_link_id=pl_meta.get("payment_link_id"),
#              pl_order_id=pl_meta.get("order_id"),
#              payment_link_url=payment_link_url)
#         # DO NOT overwrite o.razorpay_order_id here; it is already the canonical one.
#     except Exception as e:
#         pl_meta = {"error": str(e)}
#         _dbg("CREATE_ORDER:RP_PAYMENT_LINK_ERR", err=str(e))

#     if booking:
#         try:
#             booking.order = o
#             booking.save(update_fields=["order"])
#             _dbg("CREATE_ORDER:BOOKING_LINKED_TO_ORDER", booking_id=booking.id, order_db_id=o.id)
#         except Exception as e:
#             _dbg("CREATE_ORDER:BOOKING_LINK_ERR", err=str(e))

#     return Response({
#         "order": {"id": rp_order["id"], "amount": amount_paise, "currency": "INR"},
#         "key_id": getattr(settings, "RAZORPAY_KEY_ID", None),
#         "order_db": OrderSerializer(o).data,
#         "payment_link": payment_link_url,
#         "payment_link_meta": pl_meta,
#         "ticket_order_id": rp_order["id"],
#         "ticket_api_path": f"/api/tickets/{rp_order['id']}.pdf",
#         "pricing_snapshot": getattr(booking, "pricing_breakdown", None) if booking else {
#             "base": {"includes": package.base_includes, "price_inr": int(package.price_inr)},
#             "promo": promo_br,
#             "total_inr_before_promo": total_inr,
#             "total_inr": final_inr,
#         },
#     })




# -----------------------------------
# Razorpay Webhook (with allocation hook)
# -----------------------------------

@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def razorpay_webhook(request):
    secret = getattr(settings, "RAZORPAY_WEBHOOK_SECRET", None)
    if not secret:
        return HttpResponse("webhook not configured", status=503)

    body = request.body
    received_sig = request.headers.get("X-Razorpay-Signature", "")
    expected_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    # Parse JSON safely (even before signature check, so we can log what we got)
    try:
        evt = json.loads(body.decode("utf-8"))
    except Exception:
        WebhookEvent.objects.create(
            event="__parse_error__",
            signature=received_sig,
            remote_addr=request.META.get("REMOTE_ADDR"),
            payload={"raw": "invalid json"},
            raw_body=body.decode("utf-8", errors="replace"),
            processed_ok=False,
            error="Invalid JSON",
        )
        return HttpResponse("bad json", status=400)

    # Create initial log row
    log = WebhookEvent.objects.create(
        provider="razorpay",
        event=evt.get("event") or "",
        signature=received_sig,
        remote_addr=request.META.get("REMOTE_ADDR"),
        payload=evt,
        raw_body=body.decode("utf-8", errors="replace"),
        processed_ok=False,
    )

    # Verify signature
    if not hmac.compare_digest(received_sig, expected_sig):
        log.error = "invalid signature"
        log.save(update_fields=["error", "processed_at"])
        return HttpResponse("invalid signature", status=400)

    event_name = evt.get("event")
    payload = evt.get("payload", {}) or {}

    matched = None

    def mark_paid_by_order_id(order_id: str, payment_id: str | None = None):
        nonlocal matched
        if not order_id:
            return False
        try:
            o = Order.objects.get(razorpay_order_id=order_id)
            matched = o
            if not o.paid:
                o.paid = True
                if payment_id:
                    o.razorpay_payment_id = payment_id
                o.save(update_fields=["paid", "razorpay_payment_id"])
            return True
        except Order.DoesNotExist:
            return False

    try:
        if event_name == "payment.captured":
            p = (payload.get("payment") or {}).get("entity") or {}
            mark_paid_by_order_id(p.get("order_id"), p.get("id"))

        elif event_name == "payment_link.paid":
            pl = (payload.get("payment_link") or {}).get("entity") or {}
            pay = (payload.get("payment") or {}).get("entity") or {}

            # 1) reference_id => orderdb-<local_id>
            ref = pl.get("reference_id")
            if isinstance(ref, str) and ref.startswith("orderdb-"):
                try:
                    local_id = int(ref.split("-", 1)[1])
                    matched = Order.objects.get(id=local_id)
                    if not matched.paid:
                        matched.paid = True
                        if pay.get("id"):
                            matched.razorpay_payment_id = pay["id"]
                        if pl.get("order_id"):
                            matched.razorpay_order_id = pl["order_id"]
                        matched.save(update_fields=["paid", "razorpay_payment_id", "razorpay_order_id"])
                except Exception:
                    matched = None

            # 2) fallback: payment.order_id
            if matched is None:
                mark_paid_by_order_id(pay.get("order_id"), pay.get("id"))

            # 3) fallback: payment_link.order_id
            if matched is None:
                mark_paid_by_order_id(pl.get("order_id"), pay.get("id"))

            # 4) fallback: notes.local_rp_order
            if matched is None:
                notes = pl.get("notes") or {}
                if notes.get("local_rp_order"):
                    mark_paid_by_order_id(notes["local_rp_order"], pay.get("id"))

        # If we have a paid order, auto-allocate any linked booking
        if matched and matched.paid:
            try:
                booking = getattr(matched, "booking", None)
                if booking and booking.status != "CONFIRMED":
                    # If no pricing snapshot yet (order created without booking_id), compute now
                    if booking.pricing_total_inr is None:
                        total_inr, breakdown, guests_total = _compute_booking_pricing(matched.package, booking)
                        booking.pricing_total_inr = total_inr
                        booking.pricing_breakdown = breakdown
                        booking.guests = guests_total
                        booking.save(update_fields=["pricing_total_inr", "pricing_breakdown", "guests"])

                    # IMPORTANT: do NOT validate against booking.unit_type here.
                    picks = allocate_units_for_booking(booking, pkg=matched.package)

                    # tiny breadcrumb for logs
                    try:
                        labels = [getattr(u, "label", u.id) for u in picks]
                        log.error = f"{log.error or ''} | allocated_units={labels}"
                    except Exception:
                        pass
            except Exception as alloc_err:
                log.error = f"{log.error or ''} | allocate_err: {alloc_err}"

        # Success
        log.matched_order = matched
        log.processed_ok = True
        log.error = log.error or ""
        log.save(update_fields=["matched_order", "processed_ok", "error", "processed_at"])
        return HttpResponse("ok")

    except Exception as e:
        log.error = str(e)
        log.matched_order = matched
        log.save(update_fields=["error", "matched_order", "processed_at"])
        return HttpResponse("error", status=500)





# -----------------------------------
# Ticket PDF (unchanged)
# -----------------------------------




from rest_framework import renderers
from rest_framework.decorators import renderer_classes

class PassthroughPDFRenderer(renderers.BaseRenderer):
    """
    Accepts Accept: application/pdf so DRF doesn't 406 before our view runs.
    We still return HttpResponse(pdf_bytes), so this is a no-op renderer.
    """
    media_type = "application/pdf"
    format = "pdf"
    charset = None
    render_style = "binary"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data

# ---------- Tickets (PDF) ----------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
@renderer_classes([PassthroughPDFRenderer, renderers.JSONRenderer, renderers.BrowsableAPIRenderer])
def ticket_pdf(request, razorpay_order_id: str):
    _dbg("TICKET:ENTRY",
         path=getattr(request, "get_full_path", lambda: "")(),
         is_auth=getattr(request.user, "is_authenticated", False),
         user_id=getattr(request.user, "id", None),
         rp_order_id=razorpay_order_id)

    # Strictly fetch the caller’s order
    try:
        o = (Order.objects
             .select_related("user", "package", "booking", "booking__event", "booking__property")
             .get(razorpay_order_id=razorpay_order_id, user_id=request.user.id))
    except Order.DoesNotExist:
        any_o = Order.objects.filter(razorpay_order_id=razorpay_order_id).first()
        if any_o:
            _dbg("TICKET:ORDER_FOUND_WRONG_USER",
                 req_user_id=request.user.id, owner_id=any_o.user_id, rp_order_id=razorpay_order_id)
            return Response({"error": "forbidden"}, status=403)
        _dbg("TICKET:ORDER_NOT_FOUND", rp_order_id=razorpay_order_id)
        return Response({"error": "not found"}, status=404)

    if not o.paid:
        _dbg("TICKET:UNPAID", rp_order_id=razorpay_order_id)
        return Response({"error": "payment not completed"}, status=400)

    # Optional: dates/venue from booking
    travel_dates = None
    venue = "Highway to Heal"
    if getattr(o, "booking", None):
        if getattr(o.booking, "event", None) and o.booking.event.start_date:
            travel_dates = o.booking.event.start_date.strftime("%d %b %Y")
        if getattr(o.booking, "property", None) and o.booking.property.name:
            venue = o.booking.property.name

    try:
        pdf_bytes = build_invoice_and_pass_pdf_from_order(
            order=o,
            verify_url_base=getattr(settings, "TICKET_VERIFY_URL", None),
            logo_filename="Logo.png",
            pass_bg_filename="backimage.jpg",
            travel_dates=travel_dates,
            venue=venue,
        )
    except Exception as e:
        _dbg("TICKET:PDF_RENDER_FAILED", err=str(e))
        return Response({"error": "pdf_render_failed", "detail": repr(e)}, status=500)

    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename=H2H_{o.razorpay_order_id}.pdf'
    _dbg("TICKET:SUCCESS", rp_order_id=o.razorpay_order_id, user_id=o.user_id)
    return resp


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@renderer_classes([PassthroughPDFRenderer, renderers.JSONRenderer, renderers.BrowsableAPIRenderer])
def ticket_pdf_by_order_id(request, order_id: int):
    _dbg("TICKET:ENTRY_BY_ORDER_ID",
         path=getattr(request, "get_full_path", lambda: "")(),
         is_auth=getattr(request.user, "is_authenticated", False),
         user_id=getattr(request.user, "id", None),
         order_id=order_id)

    try:
        o = (Order.objects
             .select_related("user", "package", "booking", "booking__event", "booking__property")
             .get(id=order_id, user_id=request.user.id))
    except Order.DoesNotExist:
        _dbg("TICKET:ORDER_ID_NOT_FOUND", order_id=order_id)
        return Response({"error": "not found"}, status=404)

    if not o.paid:
        _dbg("TICKET:UNPAID_BY_ORDER_ID", order_id=order_id)
        return Response({"error": "payment not completed"}, status=400)

    travel_dates = None
    venue = "Highway to Heal"
    if getattr(o, "booking", None):
        if getattr(o.booking, "event", None) and o.booking.event.start_date:
            travel_dates = o.booking.event.start_date.strftime("%d %b %Y")
        if getattr(o.booking, "property", None) and o.booking.property.name:
            venue = o.booking.property.name

    try:
        pdf_bytes = build_invoice_and_pass_pdf_from_order(
            order=o,
            verify_url_base=getattr(settings, "TICKET_VERIFY_URL", None),
            logo_filename="Logo.png",
            pass_bg_filename="backimage.jpg",
            travel_dates=travel_dates,
            venue=venue,
        )
    except Exception as e:
        _dbg("TICKET:PDF_RENDER_FAILED_BY_ORDER_ID", err=str(e), order_id=order_id)
        return Response({"error": "pdf_render_failed", "detail": repr(e)}, status=500)

    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename=H2H_ORDER_{o.id}.pdf'
    _dbg("TICKET:SUCCESS_BY_ORDER_ID", order_id=o.id, user_id=o.user_id)
    return resp


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@renderer_classes([PassthroughPDFRenderer, renderers.JSONRenderer, renderers.BrowsableAPIRenderer])
def ticket_pdf_by_booking_id(request, booking_id: int):
    _dbg("TICKET:ENTRY_BY_BOOKING_ID",
         path=getattr(request, "get_full_path", lambda: "")(),
         is_auth=getattr(request.user, "is_authenticated", False),
         user_id=getattr(request.user, "id", None),
         booking_id=booking_id)

    try:
        b = (Booking.objects
             .select_related("order", "event", "property", "user")
             .get(id=booking_id, user_id=request.user.id))
    except Booking.DoesNotExist:
        _dbg("TICKET:BOOKING_NOT_FOUND", booking_id=booking_id)
        return Response({"error": "not found"}, status=404)

    if not b.order_id:
        _dbg("TICKET:BOOKING_HAS_NO_ORDER", booking_id=booking_id)
        return Response({"error": "order not found for booking"}, status=404)

    o = (Order.objects
         .select_related("user", "package", "booking", "booking__event", "booking__property")
         .get(id=b.order_id))

    if not o.paid:
        _dbg("TICKET:UNPAID_BY_BOOKING_ID", booking_id=booking_id, order_id=o.id)
        return Response({"error": "payment not completed"}, status=400)

    travel_dates = None
    venue = "Highway to Heal"
    if getattr(o, "booking", None):
        if getattr(o.booking, "event", None) and o.booking.event.start_date:
            travel_dates = o.booking.event.start_date.strftime("%d %b %Y")
        if getattr(o.booking, "property", None) and o.booking.property.name:
            venue = o.booking.property.name

    try:
        pdf_bytes = build_invoice_and_pass_pdf_from_order(
            order=o,
            verify_url_base=getattr(settings, "TICKET_VERIFY_URL", None),
            logo_filename="Logo.png",
            pass_bg_filename="backimage.jpg",
            travel_dates=travel_dates,
            venue=venue,
        )
    except Exception as e:
        _dbg("TICKET:PDF_RENDER_FAILED_BY_BOOKING_ID", err=str(e), booking_id=booking_id)
        return Response({"error": "pdf_render_failed", "detail": repr(e)}, status=500)

    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename=H2H_BOOKING_{b.id}.pdf'
    _dbg("TICKET:SUCCESS_BY_BOOKING_ID", booking_id=b.id, order_id=o.id, user_id=o.user_id)
    return resp


def _pretty_ticket_filename(order, *, kind="ORDER"):
    """
    Build a readable, safe filename:
    H2H_2025_swiss-tent_rohit-singh_ORDER_21.pdf
    """
    user_name = (getattr(order.user, "get_full_name", lambda: "")() or order.user.username or "guest").strip()
    pkg_name = (getattr(order.package, "name", "") or "package").strip()
    evt_year = getattr(getattr(getattr(order, "booking", None), "event", None), "year", None)

    parts = [
        "H2H",
        str(evt_year) if evt_year else None,
        slugify(pkg_name) or "ticket",
        slugify(user_name) or None,
        f"{kind}_{order.id}",
    ]
    base = "_".join([p for p in parts if p]) + ".pdf"
    ascii_name = base.encode("ascii", "ignore").decode() or "ticket.pdf"  # fallback
    utf8_name = quote(base)
    return ascii_name, utf8_name


# @api_view(["GET"])
# @permission_classes([IsAuthenticated])
# def ticket_pdf(request, razorpay_order_id: str):
#     # Fetch only the caller’s order
#     try:
#         o = (
#             Order.objects.select_related("user", "package")
#             .get(razorpay_order_id=razorpay_order_id, user=request.user)
#         )
#     except Order.DoesNotExist:
#         return Response({"error": "not found"}, status=404)

#     # Must be paid
#     if not o.paid:
#         return Response({"error": "payment not completed"}, status=400)

#     # Build ONE PDF: Page 1 Invoice + Page 2 Pass
#     try:
#         pdf_bytes = build_invoice_and_pass_pdf_from_order(
#             order=o,
#             verify_url_base=None,              # or your verify endpoint
#             logo_filename="Logo.png",          # exact filename & case
#             pass_bg_filename="backimage.jpg",  # background image for pass
#             travel_dates="16 Nov 2025",        # optional
#             venue="Mystic Meadow, Pahalgam, Kashmir",
#         )
#     except Exception as e:
#         # Temporary visibility to debug root cause
#         return Response({"error": "pdf_render_failed", "detail": repr(e)}, status=500)

#     resp = HttpResponse(pdf_bytes, content_type="application/pdf")
#     resp["Content-Disposition"] = f'attachment; filename=H2H_{o.razorpay_order_id}.pdf'
#     return resp

# from urllib.parse import quote
# from django.utils.text import slugify

# @api_view(["GET"])
# @permission_classes([IsAuthenticated])
# def ticket_pdf(request, razorpay_order_id: str):
#     _dbg("TICKET:ENTRY",
#          path=getattr(request, "get_full_path", lambda: "")(),
#          is_auth=getattr(request.user, "is_authenticated", False),
#          user_id=getattr(request.user, "id", None),
#          rp_order_id=razorpay_order_id)

#     # Strictly fetch the caller’s order
#     try:
#         o = (Order.objects
#              .select_related("user", "package", "booking", "booking__event", "booking__property")
#              .get(razorpay_order_id=razorpay_order_id, user_id=request.user.id))
#     except Order.DoesNotExist:
#         any_o = Order.objects.filter(razorpay_order_id=razorpay_order_id).first()
#         if any_o:
#             _dbg("TICKET:ORDER_FOUND_WRONG_USER",
#                  req_user_id=request.user.id, owner_id=any_o.user_id, rp_order_id=razorpay_order_id)
#             return Response({"error": "forbidden"}, status=403)
#         _dbg("TICKET:ORDER_NOT_FOUND", rp_order_id=razorpay_order_id)
#         return Response({"error": "not found"}, status=404)

#     if not o.paid:
#         _dbg("TICKET:UNPAID", rp_order_id=razorpay_order_id)
#         return Response({"error": "payment not completed"}, status=400)

#     # Optional: dates/venue from booking
#     travel_dates = None
#     venue = "Highway to Heal"
#     if getattr(o, "booking", None):
#         if getattr(o.booking, "event", None) and o.booking.event.start_date:
#             travel_dates = o.booking.event.start_date.strftime("%d %b %Y")
#         if getattr(o.booking, "property", None) and o.booking.property.name:
#             venue = o.booking.property.name

#     try:
#         pdf_bytes = build_invoice_and_pass_pdf_from_order(
#             order=o,
#             verify_url_base=getattr(settings, "TICKET_VERIFY_URL", None),
#             logo_filename="Logo.png",
#             pass_bg_filename="backimage.jpg",
#             travel_dates=travel_dates,
#             venue=venue,
#         )
#     except Exception as e:
#         _dbg("TICKET:PDF_RENDER_FAILED", err=str(e))
#         return Response({"error": "pdf_render_failed", "detail": repr(e)}, status=500)

#     resp = HttpResponse(pdf_bytes, content_type="application/pdf")
#     resp["Content-Disposition"] = f'attachment; filename=H2H_{o.razorpay_order_id}.pdf'
#     _dbg("TICKET:SUCCESS", rp_order_id=o.razorpay_order_id, user_id=o.user_id)
#     return resp

# @api_view(["GET"])
# @permission_classes([IsAuthenticated])
# def ticket_pdf_by_order_id(request, order_id: int):
#     _dbg("TICKET:ENTRY_BY_ORDER_ID",
#          path=getattr(request, "get_full_path", lambda: "")(),
#          is_auth=getattr(request, "is_authenticated", False),
#          user_id=getattr(request.user, "id", None),
#          order_id=order_id)

#     try:
#         o = (Order.objects
#              .select_related("user", "package", "booking", "booking__event", "booking__property")
#              .get(id=order_id, user_id=request.user.id))
#     except Order.DoesNotExist:
#         _dbg("TICKET:ORDER_ID_NOT_FOUND", order_id=order_id)
#         return Response({"error": "not found"}, status=404)

#     if not o.paid:
#         _dbg("TICKET:UNPAID_BY_ORDER_ID", order_id=order_id)
#         return Response({"error": "payment not completed"}, status=400)

#     # Optional: dates/venue from booking
#     travel_dates = None
#     venue = "Highway to Heal"
#     if getattr(o, "booking", None):
#         if getattr(o.booking, "event", None) and o.booking.event.start_date:
#             travel_dates = o.booking.event.start_date.strftime("%d %b %Y")
#         if getattr(o.booking, "property", None) and o.booking.property.name:
#             venue = o.booking.property.name

#     try:
#         pdf_bytes = build_invoice_and_pass_pdf_from_order(
#             order=o,
#             verify_url_base=getattr(settings, "TICKET_VERIFY_URL", None),
#             logo_filename="Logo.png",
#             pass_bg_filename="backimage.jpg",
#             travel_dates=travel_dates,
#             venue=venue,
#         )
#     except Exception as e:
#         _dbg("TICKET:PDF_RENDER_FAILED_BY_ORDER_ID", err=str(e), order_id=order_id)
#         return Response({"error": "pdf_render_failed", "detail": repr(e)}, status=500)

#     resp = HttpResponse(pdf_bytes, content_type="application/pdf")
#     resp["Content-Disposition"] = f'attachment; filename=H2H_ORDER_{o.id}.pdf'
#     _dbg("TICKET:SUCCESS_BY_ORDER_ID", order_id=o.id, user_id=o.user_id)
#     # ascii_name, utf8_name = _pretty_ticket_filename(o, kind="ORDER")
#     # resp = HttpResponse(pdf_bytes, content_type="application/pdf")
#     # resp["Content-Disposition"] = (
#     #     f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"
#     # )
#     return resp


# @api_view(["GET"])
# @permission_classes([IsAuthenticated])
# def ticket_pdf_by_booking_id(request, booking_id: int):
#     _dbg("TICKET:ENTRY_BY_BOOKING_ID",
#          path=getattr(request, "get_full_path", lambda: "")(),
#          is_auth=getattr(request, "is_authenticated", False),
#          user_id=getattr(request.user, "id", None),
#          booking_id=booking_id)

#     try:
#         b = (Booking.objects
#              .select_related("order", "event", "property", "user")
#              .get(id=booking_id, user_id=request.user.id))
#     except Booking.DoesNotExist:
#         _dbg("TICKET:BOOKING_NOT_FOUND", booking_id=booking_id)
#         return Response({"error": "not found"}, status=404)

#     if not b.order_id:
#         _dbg("TICKET:BOOKING_HAS_NO_ORDER", booking_id=booking_id)
#         return Response({"error": "order not found for booking"}, status=404)

#     o = (Order.objects
#          .select_related("user", "package", "booking", "booking__event", "booking__property")
#          .get(id=b.order_id))

#     if not o.paid:
#         _dbg("TICKET:UNPAID_BY_BOOKING_ID", booking_id=booking_id, order_id=o.id)
#         return Response({"error": "payment not completed"}, status=400)

#     # Optional: dates/venue from booking
#     travel_dates = None
#     venue = "Highway to Heal"
#     if getattr(o, "booking", None):
#         if getattr(o.booking, "event", None) and o.booking.event.start_date:
#             travel_dates = o.booking.event.start_date.strftime("%d %b %Y")
#         if getattr(o.booking, "property", None) and o.booking.property.name:
#             venue = o.booking.property.name

#     try:
#         pdf_bytes = build_invoice_and_pass_pdf_from_order(
#             order=o,
#             verify_url_base=getattr(settings, "TICKET_VERIFY_URL", None),
#             logo_filename="Logo.png",
#             pass_bg_filename="backimage.jpg",
#             travel_dates=travel_dates,
#             venue=venue,
#         )
#     except Exception as e:
#         _dbg("TICKET:PDF_RENDER_FAILED_BY_BOOKING_ID", err=str(e), booking_id=booking_id)
#         return Response({"error": "pdf_render_failed", "detail": repr(e)}, status=500)

#     resp = HttpResponse(pdf_bytes, content_type="application/pdf")
#     resp["Content-Disposition"] = f'attachment; filename=H2H_BOOKING_{b.id}.pdf'
#     _dbg("TICKET:SUCCESS_BY_BOOKING_ID", booking_id=b.id, order_id=o.id, user_id=o.user_id)
#     return resp



# def _pretty_ticket_filename(order, *, kind="ORDER"):
#     """
#     Build a readable, safe filename:
#     H2H_2025_swiss-tent_rohit-singh_ORDER_21.pdf
#     """
#     user_name = (getattr(order.user, "get_full_name", lambda: "")() or order.user.username or "guest").strip()
#     pkg_name = (getattr(order.package, "name", "") or "package").strip()
#     evt_year = getattr(getattr(getattr(order, "booking", None), "event", None), "year", None)

#     parts = [
#         "H2H",
#         str(evt_year) if evt_year else None,
#         slugify(pkg_name) or "ticket",
#         slugify(user_name) or None,
#         f"{kind}_{order.id}",
#     ]
#     base = "_".join([p for p in parts if p]) + ".pdf"
#     # RFC 6266: provide ASCII fallback + UTF-8 version
#     ascii_name = base.encode("ascii", "ignore").decode() or "ticket.pdf"
#     utf8_name = quote(base)
#     return ascii_name, utf8_name

from django.contrib.auth import logout as dj_logout

@api_view(["POST"])
@permission_classes([IsAuthenticated])  # you may switch to AllowAny if you want idempotent logout
def logout_view(request):
    """
    Server-side logout: clears the Django session.
    Frontend sends X-CSRFToken (you already prime it via /health).
    """
    dj_logout(request)
    # Optional: explicitly clear cookies on the response (sessionid is HttpOnly)
    resp = Response({"ok": True})
    resp.delete_cookie("sessionid", path="/")
    return resp

