from rest_framework import serializers
from django.contrib.auth.models import User
from .models import Package, Order

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name"]

class PackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Package
        fields = ["id", "name", "description", "price_inr", "active"]

class OrderSerializer(serializers.ModelSerializer):
    package = PackageSerializer(read_only=True)
    class Meta:
        model = Order
        fields = [
            "id", "package", "razorpay_order_id", "razorpay_payment_id",
            "razorpay_signature", "amount", "currency", "paid", "created_at"
        ]
