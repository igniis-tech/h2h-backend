from django.contrib import admin
from .models import UserProfile, Package, Order

admin.site.register(UserProfile)
admin.site.register(Package)
admin.site.register(Order)
