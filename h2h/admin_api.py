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

class AdminModelViewSet(viewsets.ModelViewSet):
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

class AdminBookingSerializer(BookingSerializer):
    user = UserSerializer(read_only=True)

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
