
import hmac
import hashlib
import json
from django.conf import settings
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import login
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from django.views.decorators.csrf import ensure_csrf_cookie

from .models import Package, Order, WebhookEvent
from .serializers import PackageSerializer, OrderSerializer, UserSerializer
from .auth_utils import (
    build_authorize_url,
    exchange_code_for_tokens,
    fetch_userinfo,
    get_or_create_user_from_userinfo,
)
from .pdf import build_ticket_pdf


@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def health(request):
    return Response({"ok": True})

# @api_view(["GET"])
# @permission_classes([AllowAny])
# def health(request):
#     return Response({"ok": True})


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
            "claims": info,   # <— add this line
            "state": state,
}
        )
    except Exception as e:
        return Response({"error": str(e)}, status=400)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(UserSerializer(request.user).data)


@api_view(["GET"])
@permission_classes([AllowAny])
def list_packages(request):
    qs = Package.objects.filter(active=True).order_by("price_inr")
    return Response(PackageSerializer(qs, many=True).data)


def _get_razorpay_client():
    """
    Lazily import and return a configured Razorpay client.
    Raises RuntimeError with a clear message if the SDK or keys are missing.
    """
    try:
        import razorpay  # lazy import to avoid crashing the app when pkg_resources/setuptools isn't present
    except Exception:
        raise RuntimeError("Razorpay SDK is not installed on the server.")

    key_id = getattr(settings, "RAZORPAY_KEY_ID", None)
    key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", None)
    if not key_id or not key_secret:
        raise RuntimeError("Razorpay keys are not configured (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET).")

    return razorpay.Client(auth=(key_id, key_secret))


# @api_view(["POST"])
# @permission_classes([IsAuthenticated])
# def create_order(request):
#     # Validate package
#     package_id = request.data.get("package_id")
#     if not package_id:
#         return Response({"error": "package_id required"}, status=400)
#     try:
#         package = Package.objects.get(id=package_id, active=True)
#     except Package.DoesNotExist:
#         return Response({"error": "invalid package"}, status=404)

#     # Convert to paise (assumes integer INR price_inr)
#     amount_paise = int(package.price_inr) * 100

#     # Build Razorpay order, but fail gracefully if SDK/keys missing
#     try:
#         client = _get_razorpay_client()
#     except RuntimeError as e:
#         return Response({"error": str(e)}, status=503)

#     try:
#         rp_order = client.order.create(
#             {
#                 "amount": amount_paise,
#                 "currency": "INR",
#                 "payment_capture": 1,
#                 "notes": {"package": package.name},
#             }
#         )
#     except Exception as e:
#         return Response({"error": f"Failed to create Razorpay order: {e}"}, status=502)

#     # Persist local order record
#     o = Order.objects.create(
#         user=request.user,
#         package=package,
#         razorpay_order_id=rp_order["id"],
#         amount=amount_paise,
#         currency="INR",
#     )

#     return Response(
#         {
#             "order": rp_order,  # what you pass to frontend to open the Razorpay checkout
#             "key_id": getattr(settings, "RAZORPAY_KEY_ID", None),
#             "order_db": OrderSerializer(o).data,
#         }
#     )

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_order(request):
    package_id = request.data.get("package_id")
    if not package_id:
        return Response({"error": "package_id required"}, status=400)
    try:
        package = Package.objects.get(id=package_id, active=True)
    except Package.DoesNotExist:
        return Response({"error": "invalid package"}, status=404)

    amount_paise = int(package.price_inr) * 100

    try:
        client = _get_razorpay_client()
    except RuntimeError as e:
        return Response({"error": str(e)}, status=503)

    # (A) Create the local order first (keep your current flow)
    # You *can* still create a Razorpay order for Standard Checkout fallback.
    try:
        rp_order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "payment_capture": 1,
            "notes": {"package": package.name},
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

    # (B) Create a Payment Link (NO order_id here)
    payment_link_url = None
    pl_meta = {}
    try:
        cust = {
            "name": (request.user.get_full_name() or request.user.username)[:100],
            "email": (getattr(request.user, "email", "") or None),
        }
        # remove empty keys
        cust = {k: v for k, v in cust.items() if v}

        pl_req = {
            "amount": amount_paise,
            "currency": "INR",
            "reference_id": f"orderdb-{o.id}",  # tie back to your DB row
            "description": f"H2H: {package.name}",
            "customer": cust or None,
            "notify": {"email": True, "sms": False},
            # Optional redirect after pay:
            # "callback_url": "https://h2h-backend-vpk9.vercel.app/api/health/",
            # "callback_method": "get",
            "notes": {
                "package": package.name,
                "local_rp_order": rp_order["id"],
            },
        }
        # drop None fields Razorpay may reject
        if pl_req.get("customer") is None:
            pl_req.pop("customer")

        pl = client.payment_link.create(pl_req)
        payment_link_url = pl.get("short_url")
        # If Razorpay returns an order_id on the PL entity, align your local row:
        if pl.get("order_id"):
            o.razorpay_order_id = pl["order_id"]
            o.save(update_fields=["razorpay_order_id"])
            rp_order["id"] = pl["order_id"]  # reflect the aligned id in response
        pl_meta = {"payment_link_id": pl.get("id")}
    except Exception as e:
        # surface a hint to debug in dev; keep checkout fallback working
        pl_meta = {"error": str(e)}

    return Response({
        "order": rp_order,                       # still usable for Standard Checkout
        "key_id": getattr(settings, "RAZORPAY_KEY_ID", None),
        "order_db": OrderSerializer(o).data,
        "payment_link": payment_link_url,        # ← redirect user here when present
        "payment_link_meta": pl_meta,            # optional: useful for logs/debug
    })

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
        # log bad JSON and bail
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

    event = evt.get("event")
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
        if event == "payment.captured":
            p = (payload.get("payment") or {}).get("entity") or {}
            mark_paid_by_order_id(p.get("order_id"), p.get("id"))

        elif event == "payment_link.paid":
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
                        # align RP order id if PL has one
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

        # Optionally: handle 'order.paid' if enabled in dashboard
        # elif event == "order.paid":
        #     ord_ent = (payload.get("order") or {}).get("entity") or {}
        #     mark_paid_by_order_id(ord_ent.get("id"))

        # Success
        log.matched_order = matched
        log.processed_ok = True
        log.error = ""
        log.save(update_fields=["matched_order", "processed_ok", "error", "processed_at"])
        return HttpResponse("ok")

    except Exception as e:
        log.error = str(e)
        log.matched_order = matched
        log.save(update_fields=["error", "matched_order", "processed_at"])
        return HttpResponse("error", status=500)


# @csrf_exempt
# @api_view(["POST"])
# @permission_classes([AllowAny])
# def razorpay_webhook(request):
#     # Graceful handling when webhook secret missing
#     webhook_secret = getattr(settings, "RAZORPAY_WEBHOOK_SECRET", None)
#     if not webhook_secret:
#         return HttpResponse("webhook not configured", status=503)

#     body = request.body
#     received_sig = request.headers.get("X-Razorpay-Signature", "")
#     expected_sig = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()

#     if not hmac.compare_digest(received_sig, expected_sig):
#         return HttpResponse("invalid signature", status=400)

#     evt = json.loads(body.decode("utf-8"))
#     event = evt.get("event")
#     payload = evt.get("payload", {})

#     if event == "payment.captured":
#         payment = payload.get("payment", {}).get("entity", {})
#         order_id = payment.get("order_id")
#         payment_id = payment.get("id")
#         try:
#             o = Order.objects.get(razorpay_order_id=order_id)
#             o.paid = True
#             o.razorpay_payment_id = payment_id
#             o.save(update_fields=["paid", "razorpay_payment_id"])
#         except Order.DoesNotExist:
#             # Unknown order_id in our DB; ignore gracefully
#             pass

#     return HttpResponse("ok")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ticket_pdf(request, razorpay_order_id: str):
    try:
        o = (
            Order.objects.select_related("user", "package")
            .get(razorpay_order_id=razorpay_order_id, user=request.user)
        )
    except Order.DoesNotExist:
        return Response({"error": "not found"}, status=404)

    if not o.paid:
        return Response({"error": "payment not completed"}, status=400)

    pdf_bytes = build_ticket_pdf(
        order_id=o.razorpay_order_id,
        user_name=o.user.get_full_name() or o.user.username,
        package_name=o.package.name,
        amount_inr=o.amount // 100,
    )
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f"attachment; filename=H2H_Ticket_{o.razorpay_order_id}.pdf"
    return resp
