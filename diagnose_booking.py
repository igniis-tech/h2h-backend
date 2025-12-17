
import os
import django
import sys

sys.path.append('e:/H2H/Dev/Advanced_payment/h2h-backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from h2h.models import Booking, User, Order, Unit, UnitType
from h2h.views import allocate_units_for_booking
from django.db.models import Q

def diagnose():
    print("--- DIAGNOSING BOOKING ---")
    
    # 1. Find User/Booking
    # Search for user Rohit Singh
    users = User.objects.filter(Q(first_name__icontains="Rohit") | Q(username__icontains="rohit"))
    print(f"Found {users.count()} users matching 'Rohit'")
    
    target_b = None
    
    for u in users:
        # Get latest booking
        b = Booking.objects.filter(user=u).order_by('-created_at').first()
        if b:
            print(f"User: {u.username} ({u.get_full_name()}) -> Latest Booking {b.id} | Status: {b.status} | Paid: {b.amount_paid}")
            target_b = b
            
            # Check orders
            orders = b.orders.all()
            print(f"  Orders: {[o.id for o in orders]}")
            for o in orders:
                print(f"    Order {o.id}: Paid={o.paid}, Pkg={o.package.name} (ID: {o.package.id})")
            
            # Check allocations
            allocs = b.allocations.all()
            print(f"  Allocations: {allocs.count()}")
            
            if target_b and allocs.count() == 0:
                print("  -> NO ALLOWCATIONS DETECTED. Attempting manual allocation...")
                try:
                    pkg = orders[0].package if orders else None
                    if not pkg: 
                        print("    ERROR: No package found on order.")
                        continue
                        
                    # Detailed pre-check
                    print(f"    Package Allowed Types: {[ut.name for ut in pkg.allowed_unit_types.all()]}")
                    print(f"    Booking Unit Type Preference: {b.unit_type}")
                    
                    res = allocate_units_for_booking(b, pkg=pkg)
                    print(f"    ALLOCATION RESULT: {res}")
                except Exception as e:
                    print(f"    ALLOCATION FAILED: {e}")
                    import traceback
                    traceback.print_exc()

if __name__ == "__main__":
    diagnose()
