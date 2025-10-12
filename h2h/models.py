from django.db import models
from django.contrib.auth.models import User

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    # Stable unique identifier from Cognito
    cognito_sub = models.CharField(max_length=128, unique=True)

    # From Cognito OIDC claims
    full_name = models.CharField(max_length=255, blank=True, default="")
    gender = models.CharField(max_length=32, blank=True, default="")
    phone_number = models.CharField(max_length=32, blank=True, default="")
    address = models.TextField(blank=True, default="")
    email_verified = models.BooleanField(default=False)
    phone_number_verified = models.BooleanField(default=False)

    # Bookkeeping
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} ({self.cognito_sub})"

class Package(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    price_inr = models.IntegerField()
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} - ₹{self.price_inr}"

class Order(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="orders")
    package = models.ForeignKey(Package, on_delete=models.PROTECT)
    razorpay_order_id = models.CharField(max_length=128, unique=True)
    razorpay_payment_id = models.CharField(max_length=128, blank=True, null=True)
    razorpay_signature = models.CharField(max_length=256, blank=True, null=True)
    amount = models.IntegerField()
    currency = models.CharField(max_length=8, default="INR")
    paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.razorpay_order_id} ({'PAID' if self.paid else 'UNPAID'})"




class WebhookEvent(models.Model):
    provider = models.CharField(max_length=32, default="razorpay")
    event = models.CharField(max_length=64, blank=True, default="")
    signature = models.CharField(max_length=256, blank=True, default="")
    delivery_id = models.CharField(  # if Razorpay ever adds delivery-id header
        max_length=128, blank=True, null=True, unique=False
    )
    remote_addr = models.GenericIPAddressField(blank=True, null=True)

    payload = models.JSONField()  # raw parsed JSON body
    raw_body = models.TextField(blank=True, default="")  # optional: exact bytes as text

    matched_order = models.ForeignKey(
        Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="webhooks"
    )
    processed_ok = models.BooleanField(default=False)
    error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        status = "OK" if self.processed_ok else "ERR"
        return f"[{self.provider}] {self.event} {status} ({self.created_at:%Y-%m-%d %H:%M})"