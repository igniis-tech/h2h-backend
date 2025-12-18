from rest_framework import viewsets, permissions, filters
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Sum
from .models import (
    UserProfile, Package, PackageImage, Order, WebhookEvent,
    Property, UnitType, Unit,
    Booking, Allocation,
    Event, EventDay,
    InventoryRow,
    SightseeingRegistration,
    PromoCode,
)
from .serializers import (
    UserProfileSerializer,
    PackageSerializer,
    OrderSerializer,
    PropertySerializer,
    UnitTypeSerializer,
    UnitSerializer,
    BookingSerializer,
    UserSerializer, # ✅ Add this
    EventSerializer,
    EventDaySerializer,
    PromoCodeSerializer,
    SightseeingRegistrationSerializer,
    InventoryRowSerializer,
)

class IsAdminUser(permissions.BasePermission):
    """
    Allows access only to admin users.
    """
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)

from .admin_mixins import AuditLogMixin, BulkActionMixin

class AdminModelViewSet(AuditLogMixin, BulkActionMixin, viewsets.ModelViewSet):
    permission_classes = [IsAdminUser]

class UserProfileViewSet(AdminModelViewSet):
    queryset = UserProfile.objects.all()
    serializer_class = UserProfileSerializer
    search_fields = ["user__username", "user__email", "full_name", "phone_number"]

class PackageViewSet(AdminModelViewSet):
    queryset = Package.objects.all()
    serializer_class = PackageSerializer
    filterset_fields = ["active", "promo_active"]

class OrderViewSet(AdminModelViewSet):
    queryset = Order.objects.all().select_related('user', 'package', 'booking')
    serializer_class = OrderSerializer
    filterset_fields = ["paid", "currency", "payment_type"]
    search_fields = ["razorpay_order_id", "user__email"]

class WebhookEventViewSet(AdminModelViewSet):
    queryset = WebhookEvent.objects.all()
    # Create a simple serializer for this if needed, unrelated to main app
    from rest_framework.serializers import ModelSerializer
    class Serializer(ModelSerializer):
        class Meta:
            model = WebhookEvent
            fields = "__all__"
    serializer_class = Serializer
    filterset_fields = ["provider", "processed_ok", "event"]

class PropertyViewSet(AdminModelViewSet):
    queryset = Property.objects.all()
    serializer_class = PropertySerializer
    search_fields = ["name"]

class UnitTypeViewSet(AdminModelViewSet):
    queryset = UnitType.objects.all()
    serializer_class = UnitTypeSerializer
    search_fields = ["name", "code"]

class UnitViewSet(AdminModelViewSet):
    queryset = Unit.objects.all().select_related('property', 'unit_type')
    serializer_class = UnitSerializer
    filterset_fields = ["status", "property", "unit_type", "category"]
    search_fields = ["label"]

class EventViewSet(AdminModelViewSet):
    queryset = Event.objects.all()
    serializer_class = EventSerializer
    filterset_fields = ["active", "booking_open", "year"]

class EventDayViewSet(AdminModelViewSet):
    queryset = EventDay.objects.all()
    serializer_class = EventDaySerializer
    filterset_fields = ["event"]

class PromoCodeViewSet(AdminModelViewSet):
    queryset = PromoCode.objects.all()
    serializer_class = PromoCodeSerializer
    filterset_fields = ["is_active", "kind"]

from rest_framework import serializers # ensure valid reference

class AdminBookingSerializer(BookingSerializer):
    user = UserSerializer(read_only=True)
    party_brief = serializers.SerializerMethodField()
    alloc_brief = serializers.SerializerMethodField()

    class Meta(BookingSerializer.Meta):
        fields = BookingSerializer.Meta.fields + ["party_brief", "alloc_brief"]

    def get_party_brief(self, obj):
        g_map = {"M": 0, "F": 0, "O": 0}
        m_map = {"VEG": 0, "NON_VEG": 0, "VEGAN": 0, "JAIN": 0, "OTHER": 0}

        def norm_gender(val):
            v = (val or "").strip().upper()
            return v if v in g_map else "O"

        def norm_meal(val):
            v = (val or "").strip().upper().replace("-", "").replace(" ", "")
            if v in {"VEG", "VEGETARIAN", "V"}: return "VEG"
            if v in {"NONVEG", "NONVEGETARIAN", "NV", "N", "EGG", "EGGETARIAN", "CHICKEN", "MEAT"}: return "NON_VEG"
            if v in {"VEGAN", "VG"}: return "VEGAN"
            if v == "JAIN": return "JAIN"
            return "OTHER"

        # primary
        g_map[norm_gender(getattr(obj, "primary_gender", None))] += 1
        m_map[norm_meal(getattr(obj, "primary_meal_preference", None))] += 1

        # companions
        comps = getattr(obj, "companions", None) or []
        for c in comps:
            if not isinstance(c, dict): continue
            g_map[norm_gender(c.get("gender"))] += 1
            m_map[norm_meal(c.get("meal") or c.get("meal_preference"))] += 1

        ppl = 1 + sum(1 for c in comps if isinstance(c, dict) and (c.get("name") or "").strip())
        genders = f"M{g_map['M']}/F{g_map['F']}/O{g_map['O']}"
        meal_parts = [f"{k}{v}" for k, v in m_map.items() if v]
        meals = "/".join(meal_parts) if meal_parts else "—"
        return f"{ppl} ppl • Genders {genders} • Meals {meals}"

    def get_alloc_brief(self, obj):
        # Use preloaded allocations if available, else fetch
        allocs = getattr(obj, "allocations", None)
        if allocs is None:
             # Fallback to reverse relation lookup
             allocs = obj.allocation_set.all()
        
        parts = []
        for a in allocs:
            u = a.unit
            if not u: continue
            prop = u.property.name if u.property else "—"
            utype = u.unit_type.name if u.unit_type else "—"
            label = u.label or f"Unit#{u.id}"
            parts.append(f"{prop} / {utype} / {label}")
        
        return " | ".join(parts) if parts else "—"

class BookingViewSet(AdminModelViewSet):
    queryset = Booking.objects.all().select_related('user', 'event', 'property', 'unit_type', 'promo_code').prefetch_related('orders')
    serializer_class = AdminBookingSerializer # ✅ Use new serializer
    filterset_fields = ["status", "payment_status", "event", "property"]
    search_fields = ["user__email", "id", "orders__razorpay_order_id"]

    @action(detail=False, methods=['GET'])
    def summary(self, request):
        total_paid = self.queryset.aggregate(Sum('amount_paid'))['amount_paid__sum'] or 0
        current_event_bookings = self.queryset.filter(event__active=True).count()
        return Response({
            "total_revenue_inr": total_paid,
            "bookings_count": self.queryset.count(),
            "active_event_bookings": current_event_bookings
        })

    @action(detail=False, methods=['POST'])
    def manual_create(self, request):
        """
        Create a booking manually (Counter Billing).
        params: name, phone, email (opt), property, unit_type, amount_paid, total_amount, ...
        """
        data = request.data
        name = data.get("name")
        phone = data.get("phone")
        email = data.get("email") or f"{phone}@manual.booking"
        prop_id = data.get("property")
        ut_id = data.get("unit_type")
        amount_paid = int(data.get("amount_paid") or 0)
        total_amount = int(data.get("total_amount") or 0) # Expected full price
        
        # If total_amount not provided, assume full payment (amount_paid)
        if total_amount < amount_paid:
            total_amount = amount_paid

        if not (name and phone and prop_id and ut_id):
            return Response({"error": "Missing required fields"}, status=400)

        from django.contrib.auth import get_user_model
        User = get_user_model()

        # 1. Find or Create User
        user = User.objects.filter(username=phone).first()
        if not user:
            user = User.objects.create_user(username=phone, email=email, first_name=name)
        
        # Determine Status
        status = "CONFIRMED"
        payment_status = "PENDING"
        if amount_paid >= total_amount and total_amount > 0:
            payment_status = "COMPLETED"
        elif amount_paid > 0:
            payment_status = "PARTIAL"
        
        # 2. Create Booking
        booking = Booking.objects.create(
            user=user,
            event=Event.objects.filter(active=True).first(),
            property_id=prop_id,
            unit_type_id=ut_id,
            status=status,
            payment_status=payment_status,
            amount_paid=amount_paid,
            guests=int(data.get("guests") or 1),
            category=data.get("category", ""),
            pricing_total_inr=total_amount, 
            
            # New Fields
            primary_gender=data.get("primary_gender", "O"),
            primary_meal_preference=data.get("primary_meal_preference", "VEG"),
            primary_age=data.get("primary_age"),
            blood_group=data.get("blood_group", ""),
            emergency_contact_name=data.get("emergency_contact_name", ""),
            emergency_contact_phone=data.get("emergency_contact_phone", ""),
        )

        # 3. Create Dummy Order (for record keeping & PDF)
        if amount_paid > 0:
            Order.objects.create(
                user=user,
                booking=booking,
                amount=amount_paid * 100, # paise
                amount_paid=amount_paid * 100,
                currency="INR",
                receipt=f"MANUAL_PAID_{booking.id}",
                razorpay_order_id=f"MAN_PID_{booking.id}",
                payment_type="MANUAL (CASH/UPI)",
                paid=True,
                status="paid"
            )
        
        # Create a pending/due order if partial? (Optional, maybe later)

        # 4. Auto Allocate
        if data.get("auto_allocate") == True:
            from .views import allocate_units_for_booking
            try:
                allocate_units_for_booking(booking)
            except Exception as e:
                pass 

        return Response(AdminBookingSerializer(booking).data)

    def get_permissions(self):
        if self.action == 'ticket_pdf':
            return [permissions.AllowAny()]
        return super().get_permissions()

    @action(detail=True, methods=['GET'])
    def ticket_pdf(self, request, pk=None):
        """
        Admin wrapper for ticket PDF.
        Supports ?token=XYZ for browser downloads.
        ROBUST: If no paid order, creates a dummy context for PDF generation.
        """
        # 1. Manual Auth Check
        user = request.user
        if not (user and user.is_authenticated):
            token = request.query_params.get('token')
            if token:
                from rest_framework_simplejwt.authentication import JWTAuthentication
                try:
                    validated_token = JWTAuthentication().get_validated_token(token)
                    user = JWTAuthentication().get_user(validated_token)
                except Exception:
                    pass
        
        if not (user and user.is_authenticated and user.is_staff):
            return Response({"error": "Unauthorized"}, status=401)

        from h2h.views import build_invoice_and_pass_pdf_from_order
        from django.conf import settings
        from django.http import HttpResponse

        booking = self.get_object()
        
        # Try to find a real paid order
        order = booking.orders.filter(paid=True).order_by('-created_at').first()
        
        # Fallback: If no order but booking is valid/paid manually, create a dummy object
        # that mimics an Order for the PDF builder.
        if not order:
             class DummyOrder:
                 def __init__(self, b):
                     self.id = 0
                     self.amount_paid = (b.amount_paid or 0) * 100
                     self.amount = (b.pricing_total_inr or b.amount_paid or 0) * 100
                     self.currency = "INR"
                     self.receipt = f"REF_B{b.id}"
                     self.razorpay_order_id = f"REF_B{b.id}"
                     self.payment_type = "MANUAL/UNKNOWN"
                     self.created_at = b.created_at
                     self.user = b.user
                     self.booking = b # Important link
             order = DummyOrder(booking)
        else:
            order.booking = booking 

        travel_dates = None
        venue = "Highway to Heal"
        if booking.event and booking.event.start_date:
            travel_dates = booking.event.start_date.strftime("%d %b %Y")
        if booking.property and booking.property.name:
            venue = booking.property.name

        try:
            pdf_bytes = build_invoice_and_pass_pdf_from_order(
                order=order,
                verify_url_base=getattr(settings, "TICKET_VERIFY_URL", None),
                logo_filename="Logo.png",
                pass_bg_filename="backimage.jpg",
                travel_dates=travel_dates,
                venue=venue,
            )
        except Exception as e:
            return Response({"error": str(e)}, status=500)

        resp = HttpResponse(pdf_bytes, content_type="application/pdf")
        resp["Content-Disposition"] = f'attachment; filename=H2H_BOOKING_{booking.id}.pdf'
        return resp

    @action(detail=False, methods=['GET'])
    def dashboard_stats(self, request):
        """
        Aggregated stats for the dashboard.
        """
        # Sales Timeline (Last 7 days)
        from django.utils import timezone
        import datetime
        
        today = timezone.localdate()
        sales_timeline = []
        for i in range(6, -1, -1):
            d = today - datetime.timedelta(days=i)
            # This is rough, using booking created_at
            day_total = Booking.objects.filter(
                created_at__date=d, status='CONFIRMED'
            ).aggregate(Sum('amount_paid'))['amount_paid__sum'] or 0
            sales_timeline.append({'date': d.strftime("%Y-%m-%d"), 'sales': day_total})

        # Inventory Stats
        inventory = []
        rows = InventoryRow.objects.select_related('property', 'unit_type').all()
        
        # Naive calculation: To get true availability we need complex query on Allocations.
        # But for now, let's just return Total Capacity vs Current Confirmed Bookings for that type?
        # That's hard because manual calc is needed.
        # Simplest Proxy: Just return the defined Inventory Rows for display.
        for r in rows:
            inventory.append({
                'property': r.property.name,
                'unit_type': r.unit_type.name,
                'category': r.category,
                'total': r.quantity,
                # 'occupied': ... TODO: Calculate occupancy
            })

        return Response({
             "sales_timeline": sales_timeline,
             "inventory": inventory,
             "total_bookings": Booking.objects.filter(status='CONFIRMED').count(),
             "total_revenue": Booking.objects.aggregate(Sum('amount_paid'))['amount_paid__sum'] or 0
        })

class InventoryRowViewSet(AdminModelViewSet):
    queryset = InventoryRow.objects.all().select_related('property', 'unit_type')
    serializer_class = InventoryRowSerializer
    filterset_fields = ["property", "unit_type", "category"]

class SightseeingRegistrationViewSet(AdminModelViewSet):
    queryset = SightseeingRegistration.objects.all().select_related('user', 'booking')
    serializer_class = SightseeingRegistrationSerializer
    filterset_fields = ["status", "pay_at_venue"]

# --- Router Registration Helper ---
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register(r'users', UserProfileViewSet)
router.register(r'packages', PackageViewSet)
router.register(r'orders', OrderViewSet)
router.register(r'webhooks', WebhookEventViewSet)
router.register(r'properties', PropertyViewSet)
router.register(r'unit_types', UnitTypeViewSet)
router.register(r'units', UnitViewSet)
router.register(r'events', EventViewSet)
router.register(r'event_days', EventDayViewSet)
router.register(r'promo_codes', PromoCodeViewSet)
router.register(r'bookings', BookingViewSet)
router.register(r'inventory_rows', InventoryRowViewSet)
router.register(r'sightseeing', SightseeingRegistrationViewSet)
