
# views.py
import hmac
import hashlib
import json
from datetime import date

from django.conf import settings
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.contrib.auth import login
from django.db import transaction
from django.db.models import Exists, OuterRef

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import (
    Package,
    Order,
    WebhookEvent,
    # inventory models
    Property,
    UnitType,
    Unit,
    Booking,
    Allocation,
    # events
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


# -----------------------------------
# Health
# -----------------------------------
@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def health(request):
    return Response({"ok": True})


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
    qs = Package.objects.filter(active=True).order_by("price_inr")
    return Response(PackageSerializer(qs, many=True).data)


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


# -----------------------------------
# Pricing helpers
# -----------------------------------
def _compute_booking_pricing(package: Package, booking: Booking):
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
def allocate_units_for_booking(booking: Booking):
    """
    Allocate specific units for a booking (event-scoped).
    """
    needed = max(1, booking.guests)
    total_cap = 0
    picks = []

    taken_ids = (_units_taken_for_event(booking.event).values("id")
                 if booking.event else Unit.objects.none().values("id"))

    base_qs = Unit.objects.select_for_update().filter(
        property=booking.property,
        unit_type=booking.unit_type,
        category=booking.category,
    ).exclude(id__in=taken_ids)

    # Prefer AVAILABLE
    avail = list(base_qs.filter(status="AVAILABLE").order_by("label"))
    rest = list(base_qs.exclude(status="AVAILABLE").order_by("label"))
    pool = avail + rest

    for u in pool:
        picks.append(u)
        total_cap += (u.capacity or 1)
        if total_cap >= needed:
            break

    if total_cap < needed:
        raise ValueError("Insufficient capacity for requested guests")

    for u in picks:
        Allocation.objects.create(booking=booking, unit=u)
        u.status = "OCCUPIED"
        u.save(update_fields=["status"])

    booking.status = "CONFIRMED"
    # mirror event dates if missing
    if booking.event and (not booking.check_in or not booking.check_out):
        booking.check_in = booking.event.start_date
        booking.check_out = booking.event.end_date
        booking.save(update_fields=["status", "check_in", "check_out"])
    else:
        booking.save(update_fields=["status"])

    return picks


# -----------------------------------
# Inventory / Booking endpoints (EVENT-BASED)
# -----------------------------------
@api_view(["GET"])
@permission_classes([AllowAny])
def availability(request):
    """
    Query params:
      event_id, property_id, unit_type_id, category
      (optional) package_id — validates allowed unit types for that package

    Returns free units WITHIN the event (users do not select dates).
    """
    try:
        event = Event.objects.get(id=request.GET.get("event_id"), active=True)
        prop = Property.objects.get(id=request.GET.get("property_id"))
        utype = UnitType.objects.get(id=request.GET.get("unit_type_id"))
        category = (request.GET.get("category") or "").strip().upper()
    except Exception:
        return Response({"error": "invalid params"}, status=400)

    # Optional: validate unit_type vs package
    pkg_id = request.GET.get("package_id")
    if pkg_id:
        try:
            pkg = Package.objects.get(id=pkg_id, active=True)
            _validate_package_vs_unit_type(pkg, utype)
        except Package.DoesNotExist:
            return Response({"error": "invalid package"}, status=400)
        except ValueError as ve:
            return Response({"error": str(ve)}, status=400)

    taken_ids = _units_taken_for_event(event).values_list("id", flat=True)
    free = Unit.objects.filter(property=prop, unit_type=utype, category=category).exclude(id__in=taken_ids)

    count = free.count()
    total_cap = 0
    for u in free.iterator(chunk_size=500):
        total_cap += (u.capacity or 1)

    return Response({
        "event": EventSerializer(event).data,
        "property": PropertySerializer(prop).data,
        "unit_type": UnitTypeSerializer(utype).data,
        "category": category,
        "available_units": count,
        "total_capacity": total_cap
    })


# @api_view(["POST"])
# @permission_classes([IsAuthenticated])
# def create_booking(request):
#     """
#     Creates a booking BEFORE payment (no date selection; dates come from event).

#     Body:
#     {
#       "event_id": 1,
#       "property_id": 1,
#       "unit_type_id": 2,
#       "category": "NORMAL",
#       "guests": 2,                          # total including primary
#       "blood_group": "O+",
#       "emergency_contact_name": "John",
#       "emergency_contact_phone": "+91...",
#       # Either provide ages for the EXTRA guests (beyond package.base_includes),
#       "guest_ages": [7, 17],
#       # or provide explicit counts (server rules still apply if ages are present):
#       "extra_adults": 1,
#       "extra_children_half": 0,
#       "extra_children_free": 1,
#       "package_id": 3,   # required if order_id not provided
#       "order_id": 123    # optional; if present, package is derived from the order
#     }
#     """
#     data = request.data
#     try:
#         event = Event.objects.get(id=data.get("event_id"), active=True, booking_open=True)
#         prop = Property.objects.get(id=data.get("property_id"))
#         utype = UnitType.objects.get(id=data.get("unit_type_id"))
#     except Exception:
#         return Response({"error": "invalid event/property/unit_type"}, status=400)

#     category = (data.get("category") or "").strip().upper()
#     guests_total = int(data.get("guests") or 1)
#     blood_group = (data.get("blood_group") or "").strip()[:5]
#     emer_name = (data.get("emergency_contact_name") or "").strip()[:120]
#     emer_phone = (data.get("emergency_contact_phone") or "").strip()[:32]

#     # Determine package from order or explicit package_id
#     package = None
#     order = None
#     if data.get("order_id"):
#         try:
#             order = Order.objects.get(id=data["order_id"], user=request.user)
#             package = order.package
#         except Order.DoesNotExist:
#             return Response({"error": "invalid order_id"}, status=400)
#     else:
#         if not data.get("package_id"):
#             return Response({"error": "package_id required when order_id is not provided"}, status=400)
#         try:
#             package = Package.objects.get(id=data["package_id"], active=True)
#         except Package.DoesNotExist:
#             return Response({"error": "invalid package_id"}, status=400)

#     # Validate package <-> unit_type mapping
#     try:
#         _validate_package_vs_unit_type(package, utype)
#     except ValueError as ve:
#         return Response({"error": str(ve)}, status=400)

#     # Extract extra guest info
#     guest_ages = data.get("guest_ages") or []
#     extra_adults = int(data.get("extra_adults") or 0)
#     extra_half = int(data.get("extra_children_half") or 0)
#     extra_free = int(data.get("extra_children_free") or 0)

#     # Create booking
#     booking = Booking.objects.create(
#         user=request.user,
#         event=event,
#         property=prop,
#         unit_type=utype,
#         category=category,
#         guests=max(1, guests_total),
#         status="PENDING_PAYMENT",
#         order=order if order else None,
#         # mirror dates from event for compatibility with existing code/export
#         check_in=event.start_date,
#         check_out=event.end_date,
#         # safety
#         blood_group=blood_group,
#         emergency_contact_name=emer_name,
#         emergency_contact_phone=emer_phone,
#         # guests/pricing inputs
#         guest_ages=guest_ages if isinstance(guest_ages, list) else [],
#         extra_adults=max(0, extra_adults),
#         extra_children_half=max(0, extra_half),
#         extra_children_free=max(0, extra_free),
#     )

#     return Response(BookingSerializer(booking).data, status=201)


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


# ---------- create_booking: accept & store promo code (optional) ----------
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_booking(request):
    """
    ...existing docstring...
    + "promo_code": "H2H10" (optional)  # NEW
    """
    data = request.data
    # --- existing fetch/validate event/prop/utype, etc. ---
    try:
        event = Event.objects.get(id=data.get("event_id"), active=True, booking_open=True)
        prop = Property.objects.get(id=data.get("property_id"))
        utype = UnitType.objects.get(id=data.get("unit_type_id"))
    except Exception:
        return Response({"error": "invalid event/property/unit_type"}, status=400)

    category = (data.get("category") or "").strip().upper()
    guests_total = int(data.get("guests") or 1)
    blood_group = (data.get("blood_group") or "").strip()[:5]
    emer_name = (data.get("emergency_contact_name") or "").strip()[:120]
    emer_phone = (data.get("emergency_contact_phone") or "").strip()[:32]

    # package from order or package_id (existing)
    package = None
    order = None
    if data.get("order_id"):
        try:
            order = Order.objects.get(id=data["order_id"], user=request.user)
            package = order.package
        except Order.DoesNotExist:
            return Response({"error": "invalid order_id"}, status=400)
    else:
        if not data.get("package_id"):
            return Response({"error": "package_id required when order_id is not provided"}, status=400)
        try:
            package = Package.objects.get(id=data["package_id"], active=True)
        except Package.DoesNotExist:
            return Response({"error": "invalid package_id"}, status=400)

    # Validate package <-> unit_type mapping (existing)
    try:
        _validate_package_vs_unit_type(package, utype)
    except ValueError as ve:
        return Response({"error": str(ve)}, status=400)

    # extras (existing)
    guest_ages = data.get("guest_ages") or []
    extra_adults = int(data.get("extra_adults") or 0)
    extra_half = int(data.get("extra_children_half") or 0)
    extra_free = int(data.get("extra_children_free") or 0)

    # NEW: optional promo link (no discount calculated here; snapshot happens at order)
    promo = None
    if data.get("promo_code"):
        promo = _get_live_promocode(str(data["promo_code"]))
        if not promo:
            return Response({"error": "invalid_or_expired_promocode"}, status=400)

    booking = Booking.objects.create(
        user=request.user,
        event=event,
        property=prop,
        unit_type=utype,
        category=category,
        guests=max(1, guests_total),
        status="PENDING_PAYMENT",
        order=order if order else None,
        check_in=event.start_date, check_out=event.end_date,
        blood_group=blood_group, emergency_contact_name=emer_name, emergency_contact_phone=emer_phone,
        guest_ages=guest_ages if isinstance(guest_ages, list) else [],
        extra_adults=max(0, extra_adults), extra_children_half=max(0, extra_half), extra_children_free=max(0, extra_free),
        promo_code=promo,  # NEW
    )
    return Response(BookingSerializer(booking).data, status=201)


# ---------- create_order: compute price, then apply promo BEFORE Razorpay ----------
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_order(request):
    """
    Body:
    {
      "package_id": 1,
      "booking_id": 123,         # recommended
      "promo_code": "H2H10"      # optional; if booking has a promo_code, that wins
    }
    """
    package_id = request.data.get("package_id")
    booking_id = request.data.get("booking_id")
    req_promo_code = (request.data.get("promo_code") or "").strip() or None

    if not package_id:
        return Response({"error": "package_id required"}, status=400)

    try:
        package = Package.objects.get(id=package_id, active=True)
    except Package.DoesNotExist:
        return Response({"error": "invalid package"}, status=404)

    # Default: base price
    total_inr = int(package.price_inr)
    booking = None

    if booking_id:
        try:
            booking = Booking.objects.get(id=booking_id, user=request.user)
        except Booking.DoesNotExist:
            return Response({"error": "invalid booking_id"}, status=400)

        try:
            _validate_package_vs_unit_type(package, booking.unit_type)
        except ValueError as ve:
            return Response({"error": str(ve)}, status=400)

        total_inr, breakdown, guests_total = _compute_booking_pricing(package, booking)

    # Resolve promo source: (1) booking.promo_code, else (2) request
    promo = booking.promo_code if booking and booking.promo_code_id else _get_live_promocode(req_promo_code)

    # Apply promo
    promo_discount, final_inr, promo_br = _apply_promocode(total_inr, promo)

    # If booking exists, snapshot pricing + promo onto booking BEFORE calling Razorpay
    if booking:
        # enrich breakdown with promo snapshot
        if promo_br:
            breakdown = breakdown or {}
            breakdown = {**breakdown, "promo": promo_br}
        booking.pricing_total_inr = final_inr
        booking.pricing_breakdown = breakdown or booking.pricing_breakdown
        booking.guests = guests_total if booking_id else booking.guests
        booking.promo_discount_inr = promo_discount
        booking.promo_breakdown = promo_br
        if promo and not booking.promo_code_id:
            booking.promo_code = promo
        booking.save(update_fields=[
            "pricing_total_inr", "pricing_breakdown", "guests",
            "promo_discount_inr", "promo_breakdown", "promo_code"
        ])

    amount_paise = max(1, int(final_inr)) * 100

    try:
        client = _get_razorpay_client()
    except RuntimeError as e:
        return Response({"error": str(e)}, status=503)

    # (A) Razorpay order
    try:
        rp_order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "payment_capture": 1,
            "notes": {"package": package.name, "booking_id": booking_id or "", "promo_code": promo and promo.code or ""},
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

    # (B) Payment Link (amount already discounted)
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
                "promo_code": promo and promo.code or "",
                "promo_discount_inr": promo_discount,
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

    if booking:
        booking.order = o
        booking.save(update_fields=["order"])

    return Response({
        "order": rp_order,
        "key_id": getattr(settings, "RAZORPAY_KEY_ID", None),
        "order_db": OrderSerializer(o).data,
        "payment_link": payment_link_url,
        "payment_link_meta": pl_meta,
        "pricing_snapshot": getattr(booking, "pricing_breakdown", None) if booking else {
            # if no booking, still return a small breakdown for UI
            "base": {"includes": package.base_includes, "price_inr": int(package.price_inr)},
            "promo": promo_br,
            "total_inr_before_promo": total_inr,
            "total_inr": final_inr,
        },
    })

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
                    # Final guard: ensure paid order's package matches booking's unit_type
                    try:
                        _validate_package_vs_unit_type(matched.package, booking.unit_type)
                    except ValueError as ve:
                        log.error = f"{log.error or ''} | package/type mismatch: {ve}"
                    else:
                        # If no pricing snapshot yet (order created without booking_id), compute now
                        if booking.pricing_total_inr is None:
                            total_inr, breakdown, guests_total = _compute_booking_pricing(matched.package, booking)
                            booking.pricing_total_inr = total_inr
                            booking.pricing_breakdown = breakdown
                            booking.guests = guests_total
                            booking.save(update_fields=["pricing_total_inr", "pricing_breakdown", "guests"])
                        allocate_units_for_booking(booking)
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
@api_view(["GET"])
# @permission_classes([IsAuthenticated])
def ticket_pdf(request, razorpay_order_id: str):
    # Fetch only the caller’s order
    try:
        o = (
            Order.objects.select_related("user", "package")
            .get(razorpay_order_id=razorpay_order_id, user=request.user)
        )
    except Order.DoesNotExist:
        return Response({"error": "not found"}, status=404)

    # Must be paid
    if not o.paid:
        return Response({"error": "payment not completed"}, status=400)

    # Build ONE PDF: Page 1 Invoice + Page 2 Pass
    try:
        pdf_bytes = build_invoice_and_pass_pdf_from_order(
            order=o,
            verify_url_base=None,              # or your verify endpoint
            logo_filename="Logo.png",          # exact filename & case
            pass_bg_filename="backimage.jpg",  # background image for pass
            travel_dates="16 Nov 2025",        # optional
            venue="Mystic Meadow, Pahalgam, Kashmir",
        )
    except Exception as e:
        # Temporary visibility to debug root cause
        return Response({"error": "pdf_render_failed", "detail": repr(e)}, status=500)

    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename=H2H_{o.razorpay_order_id}.pdf'
    return resp
