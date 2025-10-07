from django.urls import path
from . import views

urlpatterns = [
    path("health/", views.health),
    path("auth/sso/authorize", views.sso_authorize),
    path("auth/sso/callback", views.sso_callback),
    path("auth/me", views.me),
    path("packages", views.list_packages),
    path("payments/create-order", views.create_order),
    path("payments/webhook", views.razorpay_webhook),
    path("tickets/<str:razorpay_order_id>.pdf", views.ticket_pdf),
]
