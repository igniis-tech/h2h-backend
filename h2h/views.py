
# views.py
import hmac
import hashlib
import json
from datetime import date
from django.utils import timezone as dj_timezone
from rest_framework.decorators import authentication_classes
from .auth_cognito import CognitoJWTAuthentication
from django.conf import settings
from django.http import HttpResponseBadRequest, HttpResponseNotFound, JsonResponse, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.contrib.auth import login
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.db.models import Prefetch
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from collections import defaultdict, Counter
from rest_framework.response import Response
from urllib.parse import parse_qsl, quote, urlparse, urlunparse

import secrets
from urllib.parse import quote, urlencode
from django.shortcuts import redirect
from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from django.contrib.auth import logout as dj_logout

from django.utils.text import slugify
from urllib.parse import quote

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
    SightseeingRegistration, 
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
    refresh_with_cognito,
)
from .pdf import build_invoice_and_pass_pdf_from_order
from h2h import models
logger = logging.getLogger("h2h.create_booking")
log = logging.getLogger("h2h")
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


# def _finalize_sightseeing_if_requested(booking: Booking) -> tuple[bool, str]:
#     """
#     Create a SightseeingRegistration when user opted-in.
#     Returns (created_bool, reason_str) for logging.
#     """
#     try:
#         if not booking:
#             return (False, "no_booking")
#         if not getattr(booking, "sightseeing_opt_in", False):
#             return (False, "opt_out")
#         # idempotent
#         if SightseeingRegistration.objects.filter(booking=booking).exists():
#             return (False, "already_exists")
#         SightseeingRegistration.objects.create(
#             booking=booking,
#             user=booking.user,
#             guests=max(1, int(getattr(booking, "guests", 1) or 1)),
#         )
#         return (True, "created")
#     except Exception as e:
#         return (False, f"error:{e}")


def _finalize_sightseeing_if_requested(booking: Booking) -> tuple[bool, str]:
    """
    Create/update SightseeingRegistration if user opted-in (pending or confirmed).
    Returns (created_bool, reason_str).
    Idempotent. Also clears the pending flag once finalized.
    """
    try:
        if not booking:
            return (False, "no_booking")

        # Treat either 'opted in' or 'pending opted in' as intent to create
        intent = bool(
            getattr(booking, "sightseeing_opt_in", False)
            or getattr(booking, "sightseeing_opt_in_pending", False)
        )
        if not intent:
            return (False, "opt_out")

        # desired guests: requested count > fallback to booking.guests >= 1
        guests = int(
            getattr(booking, "sightseeing_requested_count", 0)
            or getattr(booking, "guests", 1)
            or 1
        )
        guests = max(1, min(100, guests))

        # if already exists → update & ensure CONFIRMED
        if hasattr(booking, "sightseeing") and booking.sightseeing:
            changed = False
            if booking.sightseeing.guests != guests:
                booking.sightseeing.guests = guests
                changed = True
            if booking.sightseeing.status != "CONFIRMED":
                booking.sightseeing.status = "CONFIRMED"
                changed = True
            if changed:
                booking.sightseeing.save(update_fields=["guests", "status"])

            # clear pending + lock in the intent
            if getattr(booking, "sightseeing_opt_in_pending", False) or not getattr(booking, "sightseeing_opt_in", False):
                booking.sightseeing_opt_in = True
                booking.sightseeing_opt_in_pending = False
                booking.sightseeing_requested_count = guests
                booking.save(update_fields=["sightseeing_opt_in", "sightseeing_opt_in_pending", "sightseeing_requested_count"])
            return (False, "already_exists")

        # create fresh registration
        SightseeingRegistration.objects.create(
            booking=booking,
            user=booking.user,
            guests=guests,
            pay_at_venue=True,
            status="CONFIRMED",
        )
        booking.sightseeing_opt_in = True
        booking.sightseeing_opt_in_pending = False
        booking.sightseeing_requested_count = guests
        booking.save(update_fields=["sightseeing_opt_in", "sightseeing_opt_in_pending", "sightseeing_requested_count"])
        return (True, "created")

    except Exception as e:
        return (False, f"error:{e}")



# -----------------------------------
# Health
# -----------------------------------
from django.middleware.csrf import get_token
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.views.decorators.csrf import ensure_csrf_cookie

@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def health(request):
    # sets the cookie AND returns the value so FE can send X-CSRFToken
    return Response({"ok": True, "csrfToken": get_token(request)})


def _allowed_utype_ids_for_package(pkg: Package) -> list[int]:
    # First try M2M; else fallback map by name
    ids = list(pkg.allowed_unit_types.values_list("id", flat=True))
    if ids:
        return ids
    names = _allowed_unit_types_for_package(pkg)  # uses your fallback dict
    if not names:
        return []
    return list(UnitType.objects.filter(name__in=names).values_list("id", flat=True))

# ---- Convenience fee helpers / config ----
def _calc_convenience_fee(base_inr: float, rate: float, gst: float = 0.18) -> int:
    """
    Gross-up so that after gateway deducts (rate + GST on fee),
    you still net 'base_inr'. Returns fee in INR (rounded).
    """
    if rate <= 0:
        return 0
    gross = base_inr / (1 - rate * (1 + gst))
    return int(round(gross - base_inr))

# Defaults; override in settings.py if needed
RAZORPAY_PLATFORM_FEE_RATE = getattr(settings, "RAZORPAY_PLATFORM_FEE_RATE", 0.02)  # 2% typical
RAZORPAY_PLATFORM_FEE_GST  = getattr(settings, "RAZORPAY_PLATFORM_FEE_GST", 0.18)   # 18% GST on fee
RAZORPAY_UPI_FEE_RATE      = getattr(settings, "RAZORPAY_UPI_FEE_RATE", 0.02)       # UPI MDR = 0%

def _conv_fee_breakdown(base_inr: int, rate: float, gst: float):
    """
    Return a detailed convenience fee split so it can be shown in breakdown:
      - fee_inr            : total convenience fee (platform fee + GST on it)
      - platform_fee_inr   : the gateway MDR itself
      - platform_gst_inr   : GST charged on the platform fee
      - gross_total_inr    : base + fee
    """
    if rate <= 0:
        return {"rate": rate, "gst_rate": gst,
                "fee_inr": 0, "platform_fee_inr": 0, "platform_gst_inr": 0,
                "gross_total_inr": int(base_inr)}
    # gross-up so net is base_inr after MDR+GST
    gross = int(round(base_inr / (1 - rate * (1 + gst))))
    platform_fee = int(round(gross * rate))
    platform_gst = int(round(platform_fee * gst))
    fee_total = platform_fee + platform_gst
    return {
        "rate": rate,
        "gst_rate": gst,
        "platform_fee_inr": platform_fee,
        "platform_gst_inr": platform_gst,
        "fee_inr": fee_total,
        "gross_total_inr": gross,
    }


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


from django.conf import settings

@api_view(["GET"])
@permission_classes([AllowAny])
def sso_callback(request):
    code = request.GET.get("code")
    state = request.GET.get("state")
    # use FE-provided redirect or fall back to settings
    redirect_uri = (
        request.GET.get("redirect_uri")
        or getattr(settings, "COGNITO", {}).get("REDIRECT_URI")
        or f"{request.headers.get('Origin') or f'http://{request.get_host()}'}".rstrip("/") + "/auth/callback"
    )

    if not code:
        return Response({"error": "missing code"}, status=400)

    try:
        # IMPORTANT: redeem with the SAME redirect_uri used during authorize
        tokens = exchange_code_for_tokens(code, redirect_uri=redirect_uri)
        access_token = tokens.get("access_token")
        if not access_token:
            return Response({"error": "no access_token"}, status=400)

        info = fetch_userinfo(access_token)
        user = get_or_create_user_from_userinfo(info)
        login(request, user)

        return Response({
                "user": UserSerializer(user).data,      # optional convenience
                "tokens": {
                "access_token": tokens.get("access_token"),
                "id_token": tokens.get("id_token"),
                "expires_in": tokens.get("expires_in"),
                "refresh_token": tokens.get("refresh_token"),  # present if allowed by your app client
                "token_type": "Bearer",
            },
            "state": state,
        })
    except Exception as e:
        return Response({"error": str(e)}, status=400)


@api_view(["GET"])
@authentication_classes([CognitoJWTAuthentication])
@permission_classes([IsAuthenticated])
def me(request):
    # defensive: should never get here anonymous, but keep a guard
    if not getattr(request.user, "is_authenticated", False):
        return Response({"detail": "Not authenticated"}, status=401)
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



def _sanitize_gender(s):
    """
    Normalize to 'M', 'F', or 'O' (other/unknown).
    Accepts common variants; unknown -> 'O'.
    """
    if s is None:
        return 'O'
    v = str(s).strip().lower()
    if v in ('m', 'male', 'man', 'men'):
        return 'M'
    if v in ('f', 'female', 'woman', 'women'):
        return 'F'
    # non-binary / others / unknown collapse to 'O'
    if v in ('o', 'other', 'others', 'nb', 'nonbinary', 'non-binary', 'x'):
        return 'O'
    return 'O'


def _party_gender_counts(booking) -> dict[str, int]:
    """
    Derive M/F/O counts for this booking (primary + companions).
    If primary_gender missing, treat as 'O'.
    Companion dicts may include 'gender'.
    """
    counts = Counter()
    primary = getattr(booking, 'primary_gender', None) or 'O'
    counts[_sanitize_gender(primary)] += 1

    comps = getattr(booking, 'companions', None) or []
    if isinstance(comps, list):
        for c in comps:
            g = _sanitize_gender((c or {}).get('gender'))
            counts[g] += 1

    # Only return M/F/O keys for clarity
    return {'M': counts.get('M', 0), 'F': counts.get('F', 0), 'O': counts.get('O', 0)}


def _assign_units_by_gender(units: list, gender_counts: dict[str, int]):
    """
    Greedy packer for split scenario:
      - units: list[Unit] sorted by -capacity
      - gender_counts: {'M': int, 'F': int, 'O': int}
    Returns (picked_units_list or None).
    Strategy:
      1) Work largest gender first to reduce fragmentation.
      2) For each gender, consume largest remaining units until its count is covered.
      3) If any gender can’t be covered -> fail (return None).
    """
    # quick exit if there is a single unit that can host everyone
    total_people = sum(gender_counts.values())
    for u in units:
        if (u.capacity or 1) >= total_people:
            return [u]  # single-unit case: ignore gender

    # Filter genders that have >0
    genders = [(g, n) for g, n in gender_counts.items() if n > 0]
    if not genders:
        return []

    # Sort genders by need (desc), then units by capacity (desc)
    genders.sort(key=lambda x: x[1], reverse=True)
    pool = list(units)  # copy
    assigned = []       # selected Units (no need to track which gender took which, for now)

    for g, need in genders:
        remaining = need
        idx = 0
        # always consume biggest first
        while remaining > 0 and idx < len(pool):
            u = pool[idx]
            assigned.append(u)
            remaining -= (u.capacity or 1)
            # remove the consumed unit from pool
            pool.pop(idx)
            # do not increment idx, because we popped current
        if remaining > 0:
            # not enough capacity for this gender within available units
            return None

    # Success: 'assigned' is the set of units that covers all genders with per-unit single-gender use.
    return assigned


# views.py (helpers section)

def _sanitize_meal(s: str | None) -> str:
    """
    Normalize meal strings to: VEG | NON_VEG | VEGAN | JAIN | OTHER
    Accepts common variants like 'veg', 'non-veg', 'non veg', 'egg', etc.
    """
    val = (s or "").strip().upper()
    val = val.replace("-", "").replace(" ", "")
    if val in {"VEG", "VEGETARIAN", "V"}:
        return "VEG"
    if val in {"NONVEG", "NONVEGETARIAN", "NV", "N", "EGG", "EGGETARIAN", "CHICKEN", "MEAT"}:
        return "NON_VEG"
    if val in {"VEGAN", "VG"}:
        return "VEGAN"
    if val == "JAIN":
        return "JAIN"
    return "OTHER"

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
        gender = _sanitize_gender(p.get("gender"))  # you already added this
        meal = _sanitize_meal(p.get("meal") or p.get("meal_preference"))
        if not name:
            continue
        out.append({
            "name": name,
            "age": age,
            "blood_group": bg,
            "gender": gender,
            "meal": meal,  # NEW
        })
    return out



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

# def _validate_package_vs_unit_type(package: Package, unit_type: UnitType):
#     allowed = _allowed_unit_types_for_package(package)
#     if not allowed:
#         return
#     if unit_type.name.upper() not in allowed:
#         raise ValueError(
#             f"Selected unit type '{unit_type.name}' is not allowed for package '{package.name}'. "
#             f"Allowed: {', '.join(sorted(allowed))}"
#         )

# keep your existing imports / constants

def _get_allowed_unit_type_names(package):
    # From M2M first; if empty, fall back to your map (if you have one)
    names = []
    if hasattr(package, "allowed_unit_types"):
        names = [u.name.upper() for u in package.allowed_unit_types.all()]
    if not names:
        # Optional global fallback map
        try:
            names = [s.upper() for s in (PACKAGE_UNITTYPE_MAP.get(package.name, []) or [])]
        except NameError:
            names = []
    return names

def _validate_package_vs_unit_type(package, unit_type):
    allowed = _get_allowed_unit_type_names(package)

    # No restriction → always ok
    if not allowed:
        return

    # Missing selection but restriction exists → caller must handle UX
    if unit_type is None:
        # raise a short code so caller can map to a friendly message
        raise ValueError("unit_type_required")

    if unit_type.name.upper() not in allowed:
        raise ValueError("unit_type_not_allowed")



@api_view(["GET"])
@authentication_classes([CognitoJWTAuthentication])
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

## version 4: create_order simplified to accept only booking_id

@api_view(["POST"])
@authentication_classes([CognitoJWTAuthentication])
@permission_classes([AllowAny])
def create_order(request):
    """
    Creates a Razorpay Order + Payment Link and returns the hosted checkout URL.
    Accepts optional:
      - booking_id            (compute total incl. extras)
      - pass_platform_fee     (default true)
      - assume_method         ("upi" uses UPI fee rate, else platform fee rate)
      - return_to             (absolute FE URL to land on after payment)
    """
    package_id = request.data.get("package_id")
    booking_id = request.data.get("booking_id")
    if not package_id:
        return Response({"error": "package_id required"}, status=400)

    try:
        package = Package.objects.get(id=package_id, active=True)
    except Package.DoesNotExist:
        return Response({"error": "invalid package"}, status=404)

    # ---- base amount (and booking snapshot if provided) ----
    total_inr = int(package.price_inr)
    booking = None
    if booking_id:
        try:
            booking = Booking.objects.get(id=booking_id, user=request.user)
        except Booking.DoesNotExist:
            return Response({"error": "invalid booking_id"}, status=400)

        # if only one unit type allowed, auto-pin it for this booking
        try:
            if getattr(booking, "unit_type_id", None) is None and hasattr(package, "allowed_unit_types"):
                if package.allowed_unit_types.count() == 1:
                    booking.unit_type = package.allowed_unit_types.first()
                    booking.save(update_fields=["unit_type"])
        except Exception:
            pass

        # validate mapping
        try:
            _validate_package_vs_unit_type(package, getattr(booking, "unit_type", None))
        except ValueError as ve:
            code = str(ve)
            msg = "Please select an accommodation type for this package." if code == "unit_type_required" else \
                  "The selected accommodation type is not allowed for this package." if code == "unit_type_not_allowed" else str(ve)
            return Response({"error": msg, "code": code}, status=400)

        # compute full price (+ snapshot)
        total_inr, breakdown, guests_total = _compute_booking_pricing(package, booking)
        booking.pricing_total_inr = total_inr
        booking.pricing_breakdown = breakdown
        booking.guests = guests_total
        booking.save(update_fields=["pricing_total_inr", "pricing_breakdown", "guests"])

    # ---- convenience fee gross-up ----
    pass_platform_fee = request.data.get("pass_platform_fee")
    if pass_platform_fee is None:
        pass_platform_fee = True
    if isinstance(pass_platform_fee, str):
        pass_platform_fee = pass_platform_fee.strip().lower() in ("1", "true", "yes", "y")

    assume_method = (request.data.get("assume_method") or "").strip().lower()
    rate = RAZORPAY_UPI_FEE_RATE if assume_method == "upi" else RAZORPAY_PLATFORM_FEE_RATE

    conv = _conv_fee_breakdown(total_inr, rate, RAZORPAY_PLATFORM_FEE_GST) if pass_platform_fee else None
    conv_fee_inr = int(conv["fee_inr"]) if conv else 0
    gross_inr = int(conv["gross_total_inr"]) if conv else int(total_inr)
    amount_paise = gross_inr * 100

    # ---- Razorpay client ----
    try:
        client = _get_razorpay_client()
    except RuntimeError as e:
        return Response({"error": str(e)}, status=503)

    # (A) Create Order
    try:
        rp_order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "payment_capture": 1,
            "notes": {
                "package": package.name,
                "booking_id": booking_id or "",
                "base_amount_inr": str(total_inr),
                "convenience_fee_inr": str(conv_fee_inr),
                "platform_fee_inr": str(conv["platform_fee_inr"]) if conv else "0",
                "platform_fee_gst_inr": str(conv["platform_gst_inr"]) if conv else "0",
                "assume_method": assume_method or "auto",
            },
        })
    except Exception as e:
        return Response({"error": f"Failed to create Razorpay order: {e}"}, status=502)

    # Persist local order (GROSS)
    o = Order.objects.create(
        user=request.user, package=package,
        razorpay_order_id=rp_order["id"], amount=amount_paise, currency="INR",
    )

    # (B) Create Payment Link with a proper callback
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
                "local_rp_order": rp_order["id"],
                "booking_id": str(booking_id or ""),
                "base_amount_inr": str(total_inr),
                "convenience_fee_inr": str(conv_fee_inr),
                "platform_fee_inr": str(conv["platform_fee_inr"]) if conv else "0",
                "platform_fee_gst_inr": str(conv["platform_gst_inr"]) if conv else "0",
                "assume_method": assume_method or "auto",
            },
        }
        if cust:
            pl_req["customer"] = cust

        # >>> FIX: build callback URL with local oid and optional return_to
        from django.urls import reverse
        base_cb = request.build_absolute_uri(reverse("razorpay_callback"))
        query = {"oid": o.id}
        return_to = (request.data.get("return_to") or getattr(settings, "PAYMENT_RETURN_TO", None))
        if return_to:
            query["return_to"] = return_to  # _payment_redirect_url reads 'return_to'
        callback_abs = f"{base_cb}?{urlencode(query)}"

        pl_req["callback_url"] = callback_abs
        pl_req["callback_method"] = "get"

        pl = client.payment_link.create(pl_req)
        payment_link_url = pl.get("short_url")
        if pl.get("order_id"):
            # Payment Links sometimes generate a different RP order id
            o.razorpay_order_id = pl["order_id"]
            o.save(update_fields=["razorpay_order_id"])
            rp_order["id"] = pl["order_id"]
        pl_meta = {"payment_link_id": pl.get("id")}
    except Exception as e:
        pl_meta = {"error": str(e)}

    # attach order to booking & store convenience split
    if booking:
        booking.order = o
        try:
            bd = dict(booking.pricing_breakdown or {})
            if conv:
                bd["convenience"] = {"method": assume_method or "auto", **conv}
                booking.pricing_breakdown = bd
                booking.pricing_total_inr = gross_inr
                booking.save(update_fields=["order", "pricing_breakdown", "pricing_total_inr"])
            else:
                booking.save(update_fields=["order"])
        except Exception:
            booking.save(update_fields=["order"])

    return Response({
        "order": rp_order,
        "key_id": getattr(settings, "RAZORPAY_KEY_ID", None),
        "order_db": OrderSerializer(o).data,
        "payment_link": payment_link_url,
        "payment_link_meta": pl_meta,
        "callback_url": callback_abs,
        "pricing_snapshot": getattr(booking, "pricing_breakdown", None) if booking else None,
        "base_amount_inr": int(total_inr),
        "convenience_fee_inr": int(conv_fee_inr),
        "gross_amount_inr": int(gross_inr),
        "convenience": ({"method": assume_method or "auto", **conv} if conv else None),
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
          .filter(unit_type_id__in=([pinned_ut] if pinned_ut else allowed_ids),
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

    # >>> FIX: always build the pool, regardless of the branch above
    pool = list(qs)

    if not pool:
        raise ValueError("Insufficient capacity for requested guests")

    # --- gender counts for this booking (NOT relying on primary alone) ---
    gender_counts = _party_gender_counts(booking)
    total_needed = needed

    _dbg("ALLOC:GENDER_COUNTS", M=gender_counts['M'], F=gender_counts['F'], O=gender_counts['O'], total_needed=total_needed)

    # --- group units by (property, unit_type) for cluster-first attempt ---
    clusters = defaultdict(list)
    for u in pool:
        clusters[(u.property_id, u.unit_type_id)].append(u)

    def sort_units_desc(units):
        return sorted(
            units,
            key=lambda x: (-(x.capacity or 1),
                           getattr(x.property, "name", ""),
                           getattr(x.unit_type, "name", ""),
                           x.label)
        )

    def cluster_score(key):
        prop_id, ut_id = key
        score = 0
        if booking.property_id and prop_id == booking.property_id:
            score -= 2
        if pinned_ut and ut_id == pinned_ut:
            score -= 1
        return score

    picks: list[Unit] = []

    # --- Try single-cluster fit (prefer same property / pinned unit_type) ---
    for (prop_id, ut_id), units in sorted(clusters.items(), key=lambda kv: cluster_score(kv[0])):
        units_sorted = sort_units_desc(units)

        # Fast path: any single unit can host everyone -> ignore gender entirely
        single = next((u for u in units_sorted if (u.capacity or 1) >= total_needed), None)
        if single:
            picks = [single]
            booking.property_id = prop_id
            booking.unit_type_id = ut_id
            _dbg("ALLOC:CHOICE", mode="single_unit_cluster", property_id=prop_id, unit_type_id=ut_id, unit_id=single.id)
            break

        # Split path: pack by gender in this cluster
        assigned = _assign_units_by_gender(units_sorted, gender_counts)
        if assigned and sum((u.capacity or 1) for u in assigned) >= total_needed:
            picks = assigned
            booking.property_id = prop_id
            booking.unit_type_id = ut_id
            _dbg("ALLOC:CHOICE", mode="split_cluster_gendered", property_id=prop_id, unit_type_id=ut_id,
                 unit_ids=[u.id for u in assigned])
            break

    # --- Fallback: cross-cluster (global pool) ---
    if not picks:
        pool_sorted = sort_units_desc(pool)

        # Global single-unit fit?
        single = next((u for u in pool_sorted if (u.capacity or 1) >= total_needed), None)
        if single:
            picks = [single]
            booking.property = single.property
            booking.unit_type = single.unit_type
            _dbg("ALLOC:CHOICE", mode="single_unit_global", unit_id=single.id)
        else:
            assigned = _assign_units_by_gender(pool_sorted, gender_counts)
            if assigned and sum((u.capacity or 1) for u in assigned) >= total_needed:
                picks = assigned
                booking.property = picks[0].property
                booking.unit_type = picks[0].unit_type
                _dbg("ALLOC:CHOICE", mode="split_global_gendered", unit_ids=[u.id for u in assigned])

    if not picks or sum((u.capacity or 1) for u in picks) < total_needed:
        raise ValueError("Insufficient capacity respecting gender separation")

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
            # Prefer the order's package; else allow an explicit package override
            pkg = booking.order.package if booking.order_id else None
            if not pkg and request.GET.get("package_id"):
                pkg = Package.objects.get(id=int(request.GET["package_id"]), active=True)
            if not pkg:
                return Response({"valid": False, "reason": "package_required"}, status=400)

            # 👉 enforce per-package promo toggle here too
            if getattr(pkg, "promo_active", True) is False:
                return Response({"valid": False, "reason": "promo_disabled_for_package"}, status=200)

            # server-side pricing rules
            base_inr, _, _ = _compute_booking_pricing(pkg, booking)
        except Exception:
            return Response({"valid": False, "reason": "bad_booking_or_package"}, status=400)

    elif request.GET.get("package_id"):
        try:
            pkg = Package.objects.get(id=int(request.GET["package_id"]), active=True)

            # 👉 enforce per-package promo toggle when package_id is used
            if getattr(pkg, "promo_active", True) is False:
                return Response({"valid": False, "reason": "promo_disabled_for_package"}, status=200)

            base_inr = int(pkg.price_inr)
        except Exception:
            return Response({"valid": False, "reason": "bad_package"}, status=400)

    else:
        # No amount/package/booking: just echo promo meta
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


# def _dbg(msg, **kw):
#     """Compact JSON log; avoid duplicate console prints."""
#     try:
#         payload = json.dumps(kw, default=str)[:4000]
#     except Exception:
#         payload = str(kw)
#     logger.warning("[create_booking] %s | %s", msg, payload)


def _dbg(*args, **kwargs):
    """
    Flexible debug logger:
      - _dbg("TAG", key=val, ...)
      - _dbg(key=val, ...)
      - _dbg()  -> no-op
    Never raises.
    """
    try:
        tag = args[0] if args else kwargs.pop("tag", None)
        payload = {"tag": tag} if tag is not None else {}
        payload.update(kwargs)
        # Log to std logger at DEBUG level
        try:
            log.debug(json.dumps(payload, default=str))
        except Exception:
            # Fallback to print if JSON fails
            print("[DBG]", tag, kwargs)
    except Exception:
        # Absolute last-resort no-op
        pass

@api_view(["POST"])
@authentication_classes([CognitoJWTAuthentication])
@permission_classes([AllowAny])
def create_booking(request):
    """
    Creates a booking WITHOUT requiring property_id or unit_type_id.
    We only need: event_id, package_id (or order_id), optional companions/category/etc.
    Concrete property/unit_type/units are auto-chosen after payment by the allocator.
    """
    _dbg("ENTRY", ...)

    data = request.data
    _dbg("RAW_PAYLOAD_KEYS", keys=list(data.keys()))

    # ---- validate identifiers early with explicit errors ----
    try:
        event_id = int(data.get("event_id"))
    except Exception:
        _dbg("BAD_event_id", got=data.get("event_id"))
        return Response({"error": "invalid_event_id", "detail": "event_id must be an integer"}, status=400)

    # ---- model lookups ----
    try:
        event = Event.objects.get(id=event_id, active=True, booking_open=True)
    except Event.DoesNotExist:
        _dbg("EVENT_NOT_FOUND_OR_CLOSED", event_id=event_id)
        return Response({"error": "invalid_event", "event_id": event_id}, status=400)

    # ---- basic fields ----
    category = (data.get("category") or "").strip().upper()
    companions = _normalize_companions(data.get("companions") or [])
    primary_gender = _sanitize_gender(data.get("primary_gender"))
    primary_age = _sanitize_age(data.get("primary_age"))
    primary_meal = _sanitize_meal(data.get("primary_meal") or data.get("primary_meal_preference"))  # NEW

    guests_total = int(data.get("guests") or (1 + len(companions)))
    if guests_total != (1 + len(companions)):
        _dbg("GUESTS_ADJUSTED", before=guests_total, companions_len=len(companions))
        guests_total = 1 + len(companions)

    blood_group = _sanitize_bg(data.get("blood_group"))
    emer_name = _sanitize_name(data.get("emergency_contact_name"))
    emer_phone = (data.get("emergency_contact_phone") or "").strip()[:32]
    _dbg("PRIMARY_INFO",
         blood_group=blood_group, emer_name=emer_name, emer_phone=emer_phone,
         guests_total=guests_total, companions_len=len(companions),
         primary_gender=primary_gender,primary_age=primary_age, primary_meal=primary_meal)

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
    ages_everyone = [_sanitize_age(data.get("primary_age"))]
    ages_everyone.extend([c.get("age") for c in companions])
    try:
        extra_adults, extra_half, extra_free, extra_ages_only = _extras_from_people(
            package, guests_total, ages_everyone
        )
    except Exception as ex:
        _dbg("EXTRAS_COMPUTE_FAILED", err=str(ex), ages=ages_everyone, guests_total=guests_total)
        return Response({"error": "extras_compute_failed", "detail": str(ex)}, status=400)
    _dbg("EXTRAS_COMPUTED",
         extra_adults=extra_adults, extra_half=extra_half, extra_free=extra_free,
         ages_everyone=ages_everyone)

    # ---- promo (optional) ----
    promo = None
    if data.get("promo_code"):
        code_raw = str(data["promo_code"])
        promo = _get_live_promocode(code_raw)
        if not promo:
            _dbg("INVALID_PROMO", promo_code=code_raw)
            return Response({"error": "invalid_or_expired_promocode"}, status=400)
        _dbg("PROMO_OK", code=getattr(promo, "code", None))

    # ---- create booking ----
    try:
        booking = Booking.objects.create(
            user=request.user,
            event=event,
            property=None,
            unit_type=None,
            category=category,
            guests=max(1, guests_total),
            companions=companions,            # contains per-person gender & meal now
            status="PENDING_PAYMENT",
            order=order if order else None,
            check_in=event.start_date,
            check_out=event.end_date,
            blood_group=blood_group,
            emergency_contact_name=emer_name,
            emergency_contact_phone=emer_phone,
            guest_ages=extra_ages_only,
            extra_adults=extra_adults,
            extra_children_half=extra_half,
            extra_children_free=extra_free,
            promo_code=promo,
            primary_gender=primary_gender,
            primary_age=primary_age,                     # NEW
            primary_meal_preference=primary_meal,   # NEW
        )
    except Exception as ex:
        _dbg("BOOKING_CREATE_FAILED", err=str(ex))
        return Response({"error": "booking_create_failed", "detail": str(ex)}, status=400)

    _dbg("BOOKING_CREATED", booking_id=booking.id)
    return Response(BookingSerializer(booking).data, status=201)

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

    # Parse JSON (so we can log even on bad sig)
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

    event_name = (evt.get("event") or "").strip()
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

        # ➕ NEW: also handle order.paid
        elif event_name == "order.paid":
            oent = (payload.get("order") or {}).get("entity") or {}
            mark_paid_by_order_id(oent.get("id"))

        # If we have a paid order, auto-allocate any linked booking + sightseeing
        if matched and matched.paid:
            try:
                booking = getattr(matched, "booking", None)
                if booking and booking.status != "CONFIRMED":
                    if booking.pricing_total_inr is None:
                        total_inr, breakdown, guests_total = _compute_booking_pricing(matched.package, booking)
                        booking.pricing_total_inr = total_inr
                        booking.pricing_breakdown = breakdown
                        booking.guests = guests_total
                        booking.save(update_fields=["pricing_total_inr", "pricing_breakdown", "guests"])

                    # IMPORTANT: do NOT validate against booking.unit_type here.
                    picks = allocate_units_for_booking(booking, pkg=matched.package)

                # Create sightseeing registration if user opted in (idempotent)
                created, ss_reason = _finalize_sightseeing_if_requested(booking)
                # breadcrumb in WebhookEvent row
                try:
                    labels = []
                    try:
                        labels = [getattr(u, "label", u.id) for u in (picks or [])]
                    except Exception:
                        pass
                    log.error = f"{(log.error or '')} | alloc={labels or 'n/a'} | sightseeing={created}:{ss_reason}"
                except Exception:
                    pass

            except Exception as alloc_err:
                log.error = f"{(log.error or '')} | allocate_err: {alloc_err}"

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
@authentication_classes([CognitoJWTAuthentication])
@permission_classes([AllowAny])
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
@authentication_classes([CognitoJWTAuthentication])
@permission_classes([AllowAny])
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
@permission_classes([AllowAny])
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



@api_view(["POST"])
@permission_classes([AllowAny])  # you may switch to AllowAny if you want idempotent logout
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

# h2h/views.py
import re
import secrets
from urllib.parse import quote
from django.http import HttpResponseRedirect
from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

def _cfg(key, default=None):
    return getattr(settings, "COGNITO", {}).get(key, default)

def _default_frontend_callback(request):
    origin = (
        request.headers.get("Origin")
        or f"{'https' if request.is_secure() else 'http'}://{request.get_host()}"
    ).rstrip("/")
    # change "sso/callback" to your actual FE route if different
    return f"{origin}/auth/sso/callback"

def _normalize_domain(domain: str) -> str:
    """
    Ensure the Cognito domain is absolute (with scheme) and no trailing slash.
    If settings.COGNITO['DOMAIN'] is like 'xxx.auth.ap-south-1.amazoncognito.com'
    this turns it into 'https://xxx.auth.ap-south-1.amazoncognito.com'.
    """
    d = (domain or "").strip().rstrip("/")
    if not d:
        raise RuntimeError("COGNITO.DOMAIN not configured")
    if not re.match(r"^https?://", d):
        d = f"https://{d}"
    return d

def _cognito_authorize_url(redirect_uri: str, state: str) -> str:
    domain = _normalize_domain(_cfg("DOMAIN"))
    client_id = _cfg("CLIENT_ID")
    scope = _cfg("SCOPES", "openid email profile")
    if not client_id:
        raise RuntimeError("COGNITO.CLIENT_ID not configured")
    return (
        f"{domain}/oauth2/authorize"
        f"?client_id={quote(client_id)}"
        f"&response_type=code"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        f"&scope={quote(scope)}"
        f"&state={quote(state)}"
    )

@api_view(["GET"])
@permission_classes([AllowAny])
def login_redirect(request):
    """
    Always 302 to Cognito's hosted UI.
    Accepts optional ?redirect_uri= and ?state=.
    """
    state = request.query_params.get("state") or secrets.token_urlsafe(12)
    redirect_uri = (
        request.query_params.get("redirect_uri")
        # your settings already have this:
        or _cfg("REDIRECT_URI")
        or _default_frontend_callback(request)
    )

    # optional: store state for verification in callback
    request.session["sso_expected_state"] = state

    url = _cognito_authorize_url(redirect_uri, state)
    return HttpResponseRedirect(url)





# views.py
@api_view(["POST"])
@authentication_classes([CognitoJWTAuthentication])
@permission_classes([IsAuthenticated])
def sightseeing_optin(request):
    """
    Body:
    {
      "booking_id": 123,
      "opt_in": true,           # required: true means reserve spot
      "guests": null|number     # optional; defaults to booking.guests
    }

    Behavior:
      - If order is NOT paid -> mark booking.sightseeing_opt_in_pending=True and remember requested count.
      - If order IS paid -> create a SightseeingRegistration immediately.
    """
    booking_id = request.data.get("booking_id")
    opt_in = request.data.get("opt_in")
    guests_req = request.data.get("guests")

    if booking_id is None or str(booking_id).strip() == "":
        return Response({"error": "booking_id required"}, status=400)

    # normalize boolean
    if isinstance(opt_in, str):
        opt_in = opt_in.strip().lower() in ("1", "true", "yes", "y")

    if opt_in is not True:
        # optional: allow opt-out by clearing pending flag/cancelling reg if exists
        try:
            b = Booking.objects.get(id=booking_id, user=request.user)
        except Booking.DoesNotExist:
            return Response({"error": "invalid booking_id"}, status=404)

        # If a registration exists and order is paid, cancel it.
        if getattr(b, "order_id", None) and b.order.paid and hasattr(b, "sightseeing") and b.sightseeing:
            b.sightseeing.status = "CANCELLED"
            b.sightseeing.save(update_fields=["status"])
            return Response({"ok": True, "cancelled": True})

        # Otherwise, just clear the pending flag
        if b.sightseeing_opt_in_pending:
            b.sightseeing_opt_in_pending = False
            b.sightseeing_requested_count = 0
            b.save(update_fields=["sightseeing_opt_in_pending", "sightseeing_requested_count"])
        return Response({"ok": True, "pending": False})

    # opt_in == True pathway
    try:
        b = (Booking.objects
             .select_related("order", "user")
             .get(id=booking_id, user=request.user))
    except Booking.DoesNotExist:
        return Response({"error": "invalid booking_id"}, status=404)

    # how many?
    try:
        count = int(guests_req) if guests_req is not None else int(b.guests or 1)
        count = max(1, min(100, count))
    except Exception:
        count = max(1, int(b.guests or 1))
    
    b.sightseeing_opt_in = True
    b.sightseeing_opt_in_pending = not (getattr(b, "order_id", None) and b.order.paid)
    b.sightseeing_requested_count = count
    b.save(update_fields=["sightseeing_opt_in", "sightseeing_opt_in_pending", "sightseeing_requested_count"])

    if getattr(b, "order_id", None) and b.order.paid:
        # payment done -> create immediately
        try:
            if hasattr(b, "sightseeing") and b.sightseeing:
                # idempotency
                b.sightseeing.guests = count
                b.sightseeing.status = "CONFIRMED"
                b.sightseeing.save(update_fields=["guests", "status"])
                return Response({"ok": True, "created": False, "registration_id": b.sightseeing.id})
            # create new
            _finalize_sightseeing_if_requested(b)  # uses booking.guests; set requested first
            if not (hasattr(b, "sightseeing") and b.sightseeing):
                # ensure creation with desired count
                reg = SightseeingRegistration.objects.create(
                    user=b.user, booking=b, guests=count, pay_at_venue=True, status="CONFIRMED"
                )
                return Response({"ok": True, "created": True, "registration_id": reg.id})
            # adjust guests to requested count
            b.sightseeing.guests = count
            b.sightseeing.save(update_fields=["guests"])
            return Response({"ok": True, "created": True, "registration_id": b.sightseeing.id})
        except Exception as ex:
            return Response({"error": "create_failed", "detail": str(ex)}, status=400)
    else:
        # not paid yet -> store only a pending flag (no registration row)
        b.sightseeing_opt_in_pending = True
        b.sightseeing_requested_count = count
        b.save(update_fields=["sightseeing_opt_in_pending", "sightseeing_requested_count"])
        return Response({"ok": True, "pending": True, "requested_guests": count})


def _frontend_origin(request):
    """
    Best-effort frontend origin.
    Override in settings with FRONTEND_ORIGIN = "https://your-frontend.com"
    """
    cfg = getattr(settings, "FRONTEND_ORIGIN", "").strip().rstrip("/")
    if cfg:
        return cfg
    return (request.headers.get("Origin") or f"{'https' if request.is_secure() else 'http'}://{request.get_host()}").rstrip("/")

def _payment_redirect_url(request, status: str, order: Order | None, reason: str | None = None) -> str:
    """
    Decide where to send the browser after callback.
    Priority:
      1) ?return_to=... passed in callback URL (set during create_order)
      2) settings.PAYMENT_SUCCESS_URL / PAYMENT_FAILED_URL
      3) "/"
    Always appends: payment, oid, booking_id, reason (if any).
    """
    params = request.GET if request.method == "GET" else request.POST
    return_to = params.get("return_to")

    if status == "success":
        base = return_to or getattr(settings, "PAYMENT_SUCCESS_URL", None) or "/"
    else:
        base = return_to or getattr(settings, "PAYMENT_FAILED_URL", None) or "/"

    payload = {
        "payment": status,
        "oid": getattr(order, "id", None),
        "booking_id": getattr(getattr(order, "booking", None), "id", None),
    }
    if reason and status != "success":
        payload["reason"] = reason

    return _append_params(base, payload)


def _append_params(url, extra: dict) -> str:
    parts = list(urlparse(url))
    q = dict(parse_qsl(parts[4]))
    q.update({k: v for k, v in extra.items() if v is not None})
    parts[4] = urlencode(q)
    return urlunparse(parts)



from .models import Order, Booking  # adjust if your paths differ

def _verify_plink_callback_sig(secret, pl_id, ref_id, status, payment_id, given_sig):
    """Razorpay Payment Link callback signature:
       HMAC_SHA256(pl_id|ref_id|status|payment_id, secret)
    """
    try:
        payload = "|".join([
            pl_id or "",
            ref_id or "",
            status or "",
            payment_id or "",
        ]).encode("utf-8")
        expected = hmac.new((secret or "").encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, (given_sig or ""))
    except Exception:
        return False


@csrf_exempt
@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def razorpay_callback(request):
    """
    Razorpay redirects the browser here after hosted checkout / payment link.
    We DO NOT trust the callback to reconcile money — webhook remains source of truth.
    Here we:
      • log what we got
      • try a best-effort mark-paid (idempotent)
      • bounce the user back to FE success/failure with query params
    """
    # collect params from GET/POST
    params = {}
    try: params.update(request.GET.dict())
    except Exception: pass
    try:
        if hasattr(request, "data"): params.update(dict(request.data))
        else: params.update(request.POST.dict())
    except Exception: pass

    rp_order_id   = params.get("razorpay_order_id") or params.get("order_id")
    rp_payment_id = params.get("razorpay_payment_id") or params.get("payment_id")
    rp_signature  = params.get("razorpay_signature")
    link_status   = (params.get("razorpay_link_status") or params.get("razorpay_payment_link_status") or params.get("status") or "").lower()

    # optional helper we attached as ?oid=...
    try:
        local_oid = int(params.get("oid") or 0) or None
    except Exception:
        local_oid = None

    # observability
    try:
        WebhookEvent.objects.create(
            provider="razorpay", event="callback",
            signature=rp_signature or "",
            remote_addr=request.META.get("REMOTE_ADDR"),
            payload=params,
            raw_body=(request.body.decode("utf-8", errors="replace") if request.body else ""),
            processed_ok=False,
        )
    except Exception:
        pass

    # locate local order
    o = None
    if rp_order_id:
        o = Order.objects.select_related("booking", "package").filter(razorpay_order_id=rp_order_id).first()
    if o is None and local_oid:
        o = Order.objects.select_related("booking", "package").filter(id=local_oid).first()
    if o is None:
        return HttpResponseRedirect(_payment_redirect_url(request, "failed", None, reason="order_not_found"))

    # verify signature if present (Checkout.js)
    verified = False
    verify_error = None
    if rp_order_id and rp_payment_id and rp_signature:
        try:
            sign_str = f"{rp_order_id}|{rp_payment_id}"
            expected = hmac.new(
                getattr(settings, "RAZORPAY_KEY_SECRET").encode(),
                sign_str.encode(), hashlib.sha256
            ).hexdigest()
            verified = hmac.compare_digest(expected, rp_signature)
            if not verified:
                verify_error = "bad_signature"
        except Exception as e:
            verify_error = f"verify_error:{e}"

    # some Payment Link flows only give link_status=paid → treat as success-like
    success_like = bool(verified or (link_status == "paid"))

    if success_like:
        # best-effort mark paid (webhook will also reconcile)
        try:
            changed = False
            if not o.paid:
                o.paid = True
                changed = True
            if rp_payment_id and o.razorpay_payment_id != rp_payment_id:
                o.razorpay_payment_id = rp_payment_id
                changed = True
            if changed:
                o.save(update_fields=["paid", "razorpay_payment_id"])
        except Exception:
            pass

        # optional: light allocation (webhook also runs allocator)
        try:
            b = getattr(o, "booking", None)
            if b and b.status != "CONFIRMED":
                if b.pricing_total_inr is None:
                    total, breakdown, guests_total = _compute_booking_pricing(o.package, b)
                    b.pricing_total_inr = total
                    b.pricing_breakdown = breakdown
                    b.guests = guests_total
                    b.save(update_fields=["pricing_total_inr", "pricing_breakdown", "guests"])
                allocate_units_for_booking(b, pkg=o.package)
                _finalize_sightseeing_if_requested(b)
        except Exception:
            pass

        return HttpResponseRedirect(_payment_redirect_url(request, "success", o))

    # failure/unverifiable
    reason = verify_error or (link_status or "unknown")
    return HttpResponseRedirect(_payment_redirect_url(request, "failed", o, reason=reason))


@api_view(["GET"])
@permission_classes([AllowAny])
def order_status(request):
    oid = request.GET.get("oid")
    if not oid:
        return Response({"error": "missing oid"}, status=400)
    try:
        o = Order.objects.select_related("booking").get(id=oid)
    except Order.DoesNotExist:
        return Response({"error": "not found"}, status=404)

    # Webhook should set o.paid=True, o.razorpay_payment_id, and o.booking link.
    return Response({
        "id": o.id,
        "paid": bool(getattr(o, "paid", False)),
        "booking_id": getattr(o.booking, "id", None) if hasattr(o, "booking") else getattr(o, "booking_id", None),
        "razorpay_order_id": o.razorpay_order_id,
        "payment_id": getattr(o, "razorpay_payment_id", None),
        "amount_inr": int((o.amount or 0) // 100),
        "status": getattr(o, "status", "created"),
    })



@api_view(["POST"])
@permission_classes([AllowAny])
def oauth_refresh(request):
    rt = (request.data.get("refresh_token") or "").strip()
    if not rt:
        return Response({"error": "refresh_token required"}, status=400)
    try:
        data = refresh_with_cognito(rt)
        return Response(data)
    except Exception as e:
        return Response({"error": str(e)}, status=400)
