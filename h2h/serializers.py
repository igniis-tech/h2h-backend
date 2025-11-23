
from rest_framework import serializers
from django.contrib.auth.models import User
from .models import (
    Package, PackageImage, Order, UserProfile,
    Property, UnitType, Unit,
    Booking, Allocation,
    Event, EventDay,
    InventoryRow,
    PromoCode,
    SightseeingRegistration,
)

# --- Users ---

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

# --- Catalog / Pricing ---

class UnitTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnitType
        fields = ["id", "name", "code"]
        
        
# serializers.py
class PackageImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = PackageImage
        fields = ["id", "image_url", "caption", "display_order"]


class PackageSerializer(serializers.ModelSerializer):
    allowed_unit_types = UnitTypeSerializer(many=True, read_only=True)
    images = PackageImageSerializer(many=True, read_only=True)

    class Meta:
        model = Package
        fields = [
            "id", "name", "description", "price_inr", "active",
            "promo_active",                 # <-- NEW
            "allowed_unit_types",
            "images",
            "base_includes", "extra_price_adult_inr",
            "child_free_max_age", "child_half_max_age", "child_half_multiplier",
        ]



# --- Orders ---

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
            "amount",          # NOTE: in paise (as per model)
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

# --- Inventory building blocks ---

class PropertySerializer(serializers.ModelSerializer):
    class Meta:
        model = Property
        fields = ["id", "name", "slug", "address"]

class UnitTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnitType
        fields = ["id", "name", "code"]

class UnitSerializer(serializers.ModelSerializer):
    property = PropertySerializer(read_only=True)
    unit_type = UnitTypeSerializer(read_only=True)
    class Meta:
        model = Unit
        fields = ["id", "property", "unit_type", "category", "label", "capacity", "features", "status"]

# --- NEW: Aggregated Inventory (exactly your CSV shape) ---

class InventoryRowSerializer(serializers.ModelSerializer):
    property = PropertySerializer(read_only=True)
    unit_type = UnitTypeSerializer(read_only=True)
    total_capacity = serializers.IntegerField(read_only=True)

    class Meta:
        model = InventoryRow
        fields = [
            "id",
            "property",         # PROPERTY
            "unit_type",        # TYPE
            "category",         # CATEGORY
            "quantity",         # NO OF TENT
            "capacity_per_unit",# PEOPLE SHARE PER ROOM
            "facility",         # facility
            "total_capacity",   # computed (quantity * capacity_per_unit)
        ]

# If you ever need a flat serializer (IDs instead of nested) for POST/PUT via API, you can add:
class InventoryRowWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = InventoryRow
        fields = [
            "id", "property", "unit_type", "category",
            "quantity", "capacity_per_unit", "facility",
        ]

# --- Event / Days ---

class EventDaySerializer(serializers.ModelSerializer):
    class Meta:
        model = EventDay
        fields = ["id", "date", "title", "subtitle", "description", "order"]

class EventSerializer(serializers.ModelSerializer):
    days = EventDaySerializer(many=True, read_only=True)
    class Meta:
        model = Event
        fields = [
            "id", "name", "slug", "year", "start_date", "end_date",
            "location", "description", "active", "booking_open", "days"
        ]

# --- Booking ---


class PromoCodeSerializer(serializers.ModelSerializer):  # ADD
    class Meta:
        model = PromoCode
        fields = ["code", "kind", "value", "is_active", "start_date", "end_date", "description"]


class BookingSerializer(serializers.ModelSerializer):
    property = PropertySerializer(read_only=True)
    unit_type = UnitTypeSerializer(read_only=True)
    event = EventSerializer(read_only=True)
    promo_code = PromoCodeSerializer(read_only=True)
    order = OrderSerializer(read_only=True)  # show nested order summary to "attach every order with user"

    class Meta:
        model = Booking
        fields = [
            "id", "order", "user", "event", "property", "unit_type", "category",
            "check_in", "check_out", "guests",
            "companions",  # NEW
            "blood_group", "emergency_contact_name", "emergency_contact_phone",
            "guest_ages", "extra_adults", "extra_children_half", "extra_children_free",
            "pricing_total_inr", "pricing_breakdown",
            "promo_code", "promo_discount_inr", "promo_breakdown",  # ← FIX: add comma here
            "status", "created_at",
            "primary_gender","primary_age",
            "primary_meal_preference",
        ]
        read_only_fields = [
            "status", "created_at", "pricing_total_inr", "pricing_breakdown",
            "promo_code", "promo_discount_inr", "promo_breakdown"
        ]

class SightseeingRegistrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = SightseeingRegistration
        fields = ["id", "booking", "guests", "participants", "pay_at_venue", "status", "created_at"]
