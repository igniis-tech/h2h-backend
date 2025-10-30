from django.urls import path, re_path
from . import views

urlpatterns = [
    path("", views.api_docs, name="api_docs"),
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
    path("bookings/me", views.my_bookings, name="my_bookings"),
    path("promocodes/validate", views.validate_promocode, name="validate_promocode"),
    path("tickets/order/<int:order_id>.pdf", views.ticket_pdf_by_order_id, name="ticket_pdf_by_order_id"),
    path("tickets/booking/<int:booking_id>.pdf", views.ticket_pdf_by_booking_id, name="ticket_pdf_by_booking_id"),
    path("auth/logout", views.logout_view, name="logout"),
    path("auth/sso/login", views.login_redirect, name="auth-login"),
    path("payments/razorpay/callback/", views.razorpay_callback, name="razorpay_callback"),
    path("orders/status", views.order_status, name="order_status"),
    re_path(r"^sightseeing/optin/?$", views.sightseeing_optin, name="sightseeing_optin"),
]
