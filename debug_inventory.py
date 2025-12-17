
import os
import django
from django.conf import settings
import sys

sys.path.append('e:/H2H/Dev/Advanced_payment/h2h-backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from h2h.models import UnitType, Package

def check_package():
    print("Checking Packages...")
    swiss = UnitType.objects.filter(name__icontains="SWISS TENT").first()
    if not swiss:
        print("ERROR: No 'SWISS TENT' UnitType found!")
        return

    # Find packages that should allow Swiss Tent
    pkgs = Package.objects.all()
    for p in pkgs:
        print(f"Package: {p.name} (ID: {p.id})")
        allowed = p.allowed_unit_types.all()
        print(f"  Allowed Types (M2M): {[ut.name for ut in allowed]}")
        
        # Check if mapped by name fallback
        if not allowed:
            print(f"  Fallback check: SWISS TENT allowed? {'SWISS TENT' in [u.name.upper() for u in allowed]}")

if __name__ == "__main__":
    check_package()
