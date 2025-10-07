from django.db import models
from django.contrib.auth.models import User

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    cognito_sub = models.CharField(max_length=128, unique=True)
    full_name = models.CharField(max_length=255, blank=True)

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
