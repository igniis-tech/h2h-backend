
import os
import django
import sys
from datetime import date

sys.path.append('e:/H2H/Dev/Advanced_payment/h2h-backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.contrib.auth.models import User
from h2h.models import Booking, Package, Event, UnitType
from h2h.views import allocate_units_for_booking

def reproduction():
    print("--- REPRODUCTION START ---")
    try:
        # 1. Fetch Prerequisites
        u = User.objects.first()
        pkg = Package.objects.filter(allowed_unit_types__name__icontains="SWISS TENT").first()
        if not pkg:
            # Fallback
            swiss = UnitType.objects.filter(name__icontains="SWISS TENT").first()
            if swiss:
                pkg = Package.objects.filter(allowed_unit_types=swiss).first()
        
        if not pkg:
            print("No suitable package found.")
            return

        event = Event.objects.filter(active=True).first()
        
        print(f"User: {u}")
        print(f"Package: {pkg} (Allowed: {[ut.name for ut in pkg.allowed_unit_types.all()]})")
        print(f"Event: {event}")

        # 2. Create Dummy Booking
        b = Booking.objects.create(
            user=u,
            event=event,
            status="PENDING_PAYMENT",
            guests=1,
            pricing_total_inr=10000
        )
        print(f"Created Booking: {b.id}")

        # 3. Allocating
        print("Allocating...")
        res = allocate_units_for_booking(b, pkg)
        print(f"Result: {res}")
        
        # 4. Verify
        print(f"Booking Unit Type: {b.unit_type}")
        print(f"Booking Property: {b.property}")
        
    except Exception as e:
        print(f"EXCEPTION: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    reproduction()
