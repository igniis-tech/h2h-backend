from django.contrib import admin
from .models import UserProfile, Package, Order, WebhookEvent

admin.site.site_header = "H2H Admin Panel"
admin.site.site_title = "H2H Admin"
admin.site.index_title = "Welcome to H2H Admin"


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "cognito_sub",
        "full_name",
        "gender",
        "phone_number",
        "email_verified",
        "phone_number_verified",
        "updated_at",
    )
    search_fields = (
        "user__username",
        "user__email",
        "cognito_sub",
        "full_name",
        "phone_number",
    )


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = ("name", "price_inr", "active")
    list_filter = ("active",)
    search_fields = ("name",)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "package",
        "razorpay_order_id",
        "razorpay_payment_id",
        "paid",
        "amount",
        "currency",
        "created_at",
    )
    list_filter = ("paid", "currency", "created_at")
    search_fields = (
        "razorpay_order_id",
        "razorpay_payment_id",
        "user__username",
        "user__email",
    )


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("id", "provider", "event", "processed_ok", "matched_order", "created_at")
    list_filter = ("provider", "processed_ok", "event", "created_at")
    search_fields = ("event", "signature", "delivery_id", "matched_order__razorpay_order_id")