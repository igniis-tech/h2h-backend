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
import razorpay

from .models import Package, Order
from .serializers import PackageSerializer, OrderSerializer, UserSerializer
from .auth_utils import build_authorize_url, exchange_code_for_tokens, fetch_userinfo, get_or_create_user_from_userinfo
from .pdf import build_ticket_pdf

@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return Response({"ok": True})

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
        return Response({
            "user": UserSerializer(user).data,
            "tokens": {k: v for k, v in tokens.items() if k in ("id_token", "access_token")},
            "state": state,
        })
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

def _razorpay_client():
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_order(request):
    # Graceful handling when Razorpay is not configured
    if not (getattr(settings, 'RAZORPAY_KEY_ID', None) and getattr(settings, 'RAZORPAY_KEY_SECRET', None)):
        return Response({'error': 'Razorpay is not configured yet. Please set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.'}, status=503)

    package_id = request.data.get("package_id")
    if not package_id:
        return Response({"error": "package_id required"}, status=400)
    try:
        package = Package.objects.get(id=package_id, active=True)
    except Package.DoesNotExist:
        return Response({"error": "invalid package"}, status=404)

    amount_paise = package.price_inr * 100
    client = _razorpay_client()
    order = client.order.create({
        "amount": amount_paise,
        "currency": "INR",
        "payment_capture": 1,
        "notes": {"package": package.name}
    })

    o = Order.objects.create(
        user=request.user,
        package=package,
        razorpay_order_id=order["id"],
        amount=amount_paise,
        currency="INR",
    )

    return Response({
        "order": order,
        "key_id": settings.RAZORPAY_KEY_ID,
        "order_db": OrderSerializer(o).data,
    })

@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def razorpay_webhook(request):
    # Graceful handling when webhook secret missing
    if not getattr(settings, 'RAZORPAY_WEBHOOK_SECRET', None):
        return HttpResponse('webhook not configured', status=503)

    webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
    body = request.body
    received_sig = request.headers.get("X-Razorpay-Signature", "")
    expected_sig = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_sig, expected_sig):
        return HttpResponse("invalid signature", status=400)

    evt = json.loads(body.decode("utf-8"))
    event = evt.get("event")
    payload = evt.get("payload", {})

    if event == "payment.captured":
        payment = payload.get("payment", {}).get("entity", {})
        order_id = payment.get("order_id")
        payment_id = payment.get("id")
        try:
            o = Order.objects.get(razorpay_order_id=order_id)
            o.paid = True
            o.razorpay_payment_id = payment_id
            o.save(update_fields=["paid", "razorpay_payment_id"])
        except Order.DoesNotExist:
            pass

    return HttpResponse("ok")

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ticket_pdf(request, razorpay_order_id: str):
    try:
        o = Order.objects.select_related("user", "package").get(razorpay_order_id=razorpay_order_id, user=request.user)
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
