from rest_framework import serializers
from django.contrib.auth.models import User
from .models import Package, Order, UserProfile


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = (
            "cognito_sub",
            "full_name",
            "gender",
            "phone_number",
            "address",
            "email_verified",
            "phone_number_verified",
        )


class UserSerializer(serializers.ModelSerializer):
    profile = UserProfileSerializer(read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "profile"]


class PackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Package
        fields = ["id", "name", "description", "price_inr", "active"]


class OrderSerializer(serializers.ModelSerializer):
    package = PackageSerializer(read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "package",
            "razorpay_order_id",
            "razorpay_payment_id",
            "razorpay_signature",
            "amount",
            "currency",
            "paid",
            "created_at",
        ]
        read_only_fields = [
            "razorpay_order_id",
            "razorpay_payment_id",
            "razorpay_signature",
            "paid",
            "created_at",
        ]
