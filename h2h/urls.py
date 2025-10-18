from django.urls import path
from . import views

urlpatterns = [
    path("health/", views.health, name="health"),
    path("auth/sso/authorize", views.sso_authorize, name="sso_authorize"),
    path("auth/sso/callback", views.sso_callback, name="sso_callback"),
    path("auth/me", views.me, name="me"),
    path("packages", views.list_packages, name="list_packages"),
    path("payments/create-order", views.create_order, name="create_order"),
    path("payments/webhook", views.razorpay_webhook, name="razorpay_webhook"),
    path("tickets/<str:razorpay_order_id>.pdf", views.ticket_pdf, name="ticket_pdf"),
    path("inventory/availability", views.availability, name="availability"),
    path("bookings/create", views.create_booking, name="create_booking"),
    path("promocodes/validate", views.validate_promocode, name="validate_promocode"),  # ADD
]
