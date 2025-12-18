#models.py
from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify
from datetime import date
from django.utils import timezone
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
import builtins 


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
    """
    Base package covers 'base_includes' people (default 1).
    Extra guests are charged using the fields below, with child bands configurable.
    """
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    price_inr = models.IntegerField()
    active = models.BooleanField(default=True)
    
    promo_active = models.BooleanField(
        default=True,
        help_text="If OFF, promo codes cannot be applied to this package."
    )

    # NEW: attach multiple unit types (optional; if empty, your fallback map is used)
    allowed_unit_types = models.ManyToManyField(
        "UnitType",
        blank=True,
        related_name="packages",
        help_text="If empty, fallback PACKAGE_UNITTYPE_MAP is used."
    )

    # ---- pricing controls editable from Admin ----
    base_includes = models.PositiveSmallIntegerField(default=1, help_text="People included in base price")
    extra_price_adult_inr = models.IntegerField(
        default=0,
        help_text="Extra price per additional ADULT. If 0, base price is used as extra adult price."
    )
    child_free_max_age = models.PositiveSmallIntegerField(default=5, help_text="Age <= this is free")
    child_half_max_age = models.PositiveSmallIntegerField(default=15, help_text="Age <= this is half (and > free)")
    child_half_multiplier = models.FloatField(default=0.5, help_text="Half-price multiplier (typically 0.5)")

    def __str__(self):
        return f"{self.name} - ₹{self.price_inr}"



class PackageImage(models.Model):
    package = models.ForeignKey(Package, on_delete=models.CASCADE, related_name="images")
    image_url = models.URLField(max_length=500, help_text="Supabase public URL or signed URL for this image")
    caption = models.CharField(max_length=200, blank=True, default="")
    display_order = models.PositiveSmallIntegerField(default=0, help_text="Lower values appear first")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["display_order", "id"]

    def __str__(self):
        return f"{self.package.name} image #{self.display_order or 0}"


class Order(models.Model):
    PAYMENT_TYPE_CHOICES = (
        ("FULL", "Full Payment"),
        ("ADVANCE", "Advance Payment"),
        ("BALANCE", "Balance Payment"),
        ("REFUND", "Refund"),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="orders")
    package = models.ForeignKey(Package, on_delete=models.PROTECT)
    
    # NEW link: Many orders (txns) -> One Booking
    booking = models.ForeignKey("Booking", on_delete=models.CASCADE, related_name="orders", null=True, blank=True)
    payment_type = models.CharField(max_length=20, choices=PAYMENT_TYPE_CHOICES, default="FULL")

    razorpay_order_id = models.CharField(max_length=128, unique=True)
    razorpay_payment_id = models.CharField(max_length=128, blank=True, null=True)
    razorpay_signature = models.CharField(max_length=256, blank=True, null=True)
    amount = models.IntegerField(help_text="Amount in paise")
    currency = models.CharField(max_length=8, default="INR")
    paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.razorpay_order_id} ({'PAID' if self.paid else 'UNPAID'} - {self.payment_type})"


class WebhookEvent(models.Model):
    provider = models.CharField(max_length=32, default="razorpay")
    event = models.CharField(max_length=64, blank=True, default="")
    signature = models.CharField(max_length=256, blank=True, default="")
    delivery_id = models.CharField(max_length=128, blank=True, null=True, unique=False)
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


class Property(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    address = models.TextField(blank=True, default="")

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class UnitType(models.Model):
    """
    Examples: 'DOME TENT', 'SWISS TENT', 'COTTAGE', 'HUT'
    """
    name = models.CharField(max_length=60, unique=True)
    code = models.CharField(max_length=12, unique=True, help_text="Short code, e.g. DT, ST, CT, HUT")

    def __str__(self):
        return f"{self.name} ({self.code})"


class Unit(models.Model):
    STATUS = (
        ("AVAILABLE", "Available"),
        ("HOLD", "Hold"),
        ("OCCUPIED", "Occupied"),
        ("MAINTENANCE", "Maintenance"),
    )
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="units")
    unit_type = models.ForeignKey(UnitType, on_delete=models.PROTECT, related_name="units")
    category = models.CharField(max_length=60, blank=True, default="")  # e.g., NORMAL, LUXURY, AC DELUXE, etc.
    label = models.CharField(max_length=40, help_text="Visible code/number for the unit (unique per property)")
    capacity = models.PositiveSmallIntegerField(default=2)
    features = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS, default="AVAILABLE")

    class Meta:
        unique_together = (("property", "label"),)
        indexes = [
            models.Index(fields=["property", "unit_type", "category", "status"]),
        ]

    def __str__(self):
        return f"{self.property.name} • {self.unit_type.name} • {self.category or '-'} • {self.label}"


class InventoryRow(models.Model):
    """
    Aggregated inventory exactly matching your CSV shape:
    PROPERTY, TYPE, CATEGORY, NO OF TENT, PEOPLE SHARE PER ROOM, facility
    """
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="inventory_rows")
    unit_type = models.ForeignKey(UnitType, on_delete=models.PROTECT, related_name="inventory_rows")
    category = models.CharField(max_length=60, blank=True, default="")  # e.g., NORMAL / LUXURY / B TYPE / C TYPE
    quantity = models.PositiveIntegerField(default=0, help_text="NO OF TENT / total units for this slice")
    capacity_per_unit = models.PositiveSmallIntegerField(default=1, help_text="PEOPLE SHARE PER ROOM")
    facility = models.TextField(blank=True, default="")

    class Meta:
        unique_together = (("property", "unit_type", "category"),)
        indexes = [
            models.Index(fields=["property", "unit_type", "category"]),
        ]

    def __str__(self):
        return f"{self.property.name} • {self.unit_type.name} • {self.category or '-'} • qty={self.quantity}"

    @builtins.property
    def total_capacity(self) -> int:
        return (self.quantity or 0) * (self.capacity_per_unit or 0)

class Event(models.Model):
    """
    One H2H edition (e.g., 'H2H 2025').
    """
    name = models.CharField(max_length=120)            # e.g., "Highway to Heal 2025"
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    year = models.PositiveIntegerField()
    start_date = models.DateField()
    end_date = models.DateField()
    location = models.CharField(max_length=200, blank=True, default="")
    description = models.TextField(blank=True, default="")
    active = models.BooleanField(default=True)         # toggle current event
    booking_open = models.BooleanField(default=True)   # allow/disallow bookings

    class Meta:
        unique_together = (("name", "year"),)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(f"{self.name}-{self.year}")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.year})"


class EventDay(models.Model):
    """
    Daily schedule for an event; editable from Admin.
    """
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="days")
    date = models.DateField()
    title = models.CharField(max_length=120, blank=True, default="")      # e.g., "Trails & Melodies"
    subtitle = models.CharField(max_length=200, blank=True, default="")
    description = models.TextField(blank=True, default="")
    order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["order", "date"]
        unique_together = (("event", "date"),)

    def __str__(self):
        return f"{self.event.year} • {self.date} • {self.title or 'Day'}"

class PromoCode(models.Model):  # ADD
    KIND = (
        ("PERCENT", "Percent %"),
        ("FLAT", "Flat INR"),
    )
    code = models.CharField(max_length=40, unique=True, help_text="Case-insensitive")
    kind = models.CharField(max_length=10, choices=KIND)
    value = models.PositiveIntegerField(help_text="If PERCENT, 1–100; if FLAT, INR amount")
    is_active = models.BooleanField(default=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["code"])]

    def __str__(self):
        v = f"{self.value}% " if self.kind == "PERCENT" else f"₹{self.value} "
        return f"{self.code} ({v.strip()} | {'ON' if self.is_active else 'OFF'})"

    def is_live_today(self) -> bool:
        if not self.is_active:
            return False
        today = timezone.localdate()
        if self.start_date and today < self.start_date:
            return False
        if self.end_date and today > self.end_date:
            return False
        return True

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.kind == "PERCENT" and self.value > 100:
            raise ValidationError("Percent value cannot exceed 100.")
        

class Booking(models.Model):
    """
    One booking per order (create before payment; dates come from Event).
    """
    STATUS = (
        ("PENDING_PAYMENT", "Pending Payment"),
        ("PARTIAL", "Partially Paid"),   # NEW
        ("CONFIRMED", "Confirmed"),      # Fully paid or Enough for confirmation
        ("CANCELLED", "Cancelled"),
    )
    
    PAYMENT_STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("PARTIAL", "Partially Paid"),
        ("COMPLETED", "Completed"),
    )
    
    MEAL_CHOICES = [
        ("VEG", "Vegetarian"),
        ("NON_VEG", "Non-Vegetarian"),
        ("VEGAN", "Vegan"),
        ("JAIN", "Jain"),
        ("OTHER", "Other / Unspecified"),
    ]


    promo_code = models.ForeignKey("PromoCode", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="bookings")
    promo_discount_inr = models.IntegerField(null=True, blank=True)
    promo_breakdown = models.JSONField(null=True, blank=True)

    # REMOVED: order = OneToOneField(...) 
    # Replaced by reverse relation `orders` from Order model
    
    amount_paid = models.IntegerField(default=0, help_text="Total INR paid so far (sum of successful orders)")
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default="PENDING")
    
    sightseeing_opt_in_pending = models.BooleanField(default=False)
    sightseeing_requested_count = models.PositiveSmallIntegerField(default=0)
    sightseeing_opt_in = models.BooleanField(default=False)

    # who & what
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="bookings")
    event = models.ForeignKey("Event", on_delete=models.PROTECT, related_name="bookings", null=True, blank=True)

    # inventory slice
    # property = models.ForeignKey("Property", on_delete=models.PROTECT, related_name="bookings")
    # unit_type = models.ForeignKey("UnitType", on_delete=models.PROTECT, related_name="bookings")
    property = models.ForeignKey("Property", on_delete=models.PROTECT, related_name="bookings",
                             null=True, blank=True)
    unit_type = models.ForeignKey("UnitType", on_delete=models.PROTECT, related_name="bookings",
                              null=True, blank=True)

    category = models.CharField(max_length=60, blank=True, default="")  # align with Unit.category

    # dates for compatibility (auto-filled from event)
    check_in = models.DateField(null=True, blank=True)
    check_out = models.DateField(null=True, blank=True)

    # guests
    guests = models.PositiveSmallIntegerField(default=1, help_text="Total people including the primary person")

    # NEW: full companion list (excluding the primary person)
    # shape: [{"name":"...","age":12,"blood_group":"A+"}, ...]
    companions = models.JSONField(null=True, blank=True, help_text="List of co-travellers excluding primary user")

    # optional guest details for pricing (all extras beyond base_includes)
    guest_ages = models.JSONField(null=True, blank=True, help_text="List of ages for EXTRA guests only")
    # models.py (inside Booking)
    primary_gender = models.CharField(max_length=1, choices=[('M','Male'),('F','Female'),('O','Other')], default='O')

    extra_adults = models.PositiveSmallIntegerField(default=0)
    extra_children_half = models.PositiveSmallIntegerField(default=0)
    extra_children_free = models.PositiveSmallIntegerField(default=0)
    
    primary_age = models.PositiveSmallIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(120)],
        help_text="Age of the primary guest in years"
    )
    
    primary_meal_preference = models.CharField(
        max_length=10, choices=MEAL_CHOICES, default="NON_VEG"
    )

    # health/safety of primary
    blood_group = models.CharField(max_length=5, blank=True, default="")
    emergency_contact_name = models.CharField(max_length=120, blank=True, default="")
    emergency_contact_phone = models.CharField(max_length=32, blank=True, default="")
    
    

    # pricing snapshot
    pricing_total_inr = models.IntegerField(null=True, blank=True)
    pricing_breakdown = models.JSONField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS, default="PENDING_PAYMENT")
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        p = getattr(self.property, "name", "-")
        ut = getattr(self.unit_type, "name", "-")
        ev = self.event.year if self.event else "-"
        return f"Booking #{self.id} (Event={ev} {p} {ut} {self.category})"

    # def __str__(self):
    #     return f"Booking #{self.id} (Event={self.event and self.event.year} {self.property.name} {self.unit_type.name} {self.category})"

    @builtins.property
    def nights(self) -> int:
        if self.event and isinstance(self.event.start_date, date) and isinstance(self.event.end_date, date):
            return max(1, (self.event.end_date - self.event.start_date).days)
        if isinstance(self.check_in, date) and isinstance(self.check_out, date):
            return max(1, (self.check_out - self.check_in).days)
        return 1

class Allocation(models.Model):
    """
    Actual assignment of units to a booking (can be multiple units to meet guest capacity).
    """
    booking = models.ForeignKey("Booking", on_delete=models.CASCADE, related_name="allocations")
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, related_name="allocations")
    seats     = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def seats_used(self) -> int:
        # when old rows have seats=0, count as full capacity
        return self.seats or (self.unit.capacity or 1)

    class Meta:
        unique_together = (("booking", "unit"),)

    def __str__(self):
        return f"Allocation #{self.id}: {self.unit} -> Booking {self.booking_id}"




class SightseeingRegistration(models.Model):
    STATUS_CHOICES = (
        ("CONFIRMED", "Confirmed"),
        ("CANCELLED", "Cancelled"),
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sightseeing_regs")
    booking = models.OneToOneField("Booking", on_delete=models.CASCADE, related_name="sightseeing")
    guests = models.PositiveSmallIntegerField(default=1)
    participants = models.JSONField(blank=True, null=True, help_text="Optional snapshot of names/genders/meals")
    pay_at_venue = models.BooleanField(default=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="CONFIRMED")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Sightseeing #{self.id} · booking={self.booking_id} · guests={self.guests}"


class AuditLog(models.Model):
    ACTION_CHOICES = (
        ("CREATE", "Create"),
        ("UPDATE", "Update"),
        ("DELETE", "Delete"),
        ("BULK", "Bulk Action"),
    )
    
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="audit_logs")
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    model_name = models.CharField(max_length=100)
    object_id = models.CharField(max_length=100)
    object_repr = models.CharField(max_length=200, blank=True)
    changes = models.JSONField(default=dict)
    timestamp = models.DateTimeField(auto_now_add=True)
    remote_addr = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.timestamp} - {self.actor} - {self.action} {self.model_name} #{self.object_id}"