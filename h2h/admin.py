# admin.py
from django.contrib import admin
from django.http import HttpResponse
from django import forms
from django.urls import path
from django.shortcuts import render, redirect
from django.contrib import messages
from django.db import transaction
from django.utils.text import slugify
import csv, io, re
import pkg_resources
from django.db import models as dj_models
from django.forms import Textarea
from django.db.models import Prefetch

from .models import (
    UserProfile, Package, PackageImage, Order, WebhookEvent,
    Property, UnitType, Unit,
    Booking, Allocation,
    Event, EventDay,
    InventoryRow,
    PromoCode,
    SightseeingRegistration,
)


admin.site.site_header = "H2H Admin Panel"
admin.site.site_title = "H2H Admin"
admin.site.index_title = "Welcome to H2H Admin"


# -------------------------
# Basic model registrations
# -------------------------
@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user", "cognito_sub", "full_name", "gender", "phone_number",
        "email_verified", "phone_number_verified", "updated_at",
    )
    search_fields = ("user__username", "user__email", "cognito_sub", "full_name", "phone_number")





# admin.py
class PackageImageInline(admin.TabularInline):
    model = PackageImage
    extra = 1
    fields = ("image_url", "caption", "display_order")


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = (
        "name", "price_inr", "active", "promo_active",
        "base_includes", "extra_price_adult_inr",
        "child_free_max_age", "child_half_max_age", "child_half_multiplier",
    )
    list_filter = ("active", "promo_active")
    search_fields = ("name",)
    filter_horizontal = ("allowed_unit_types",)

    inlines = [PackageImageInline]

    fieldsets = (
        (None, {"fields": ("name", "description", "active", "promo_active")}),
        ("Unit selection", {
            "fields": ("allowed_unit_types",),
            "description": "Choose one or more unit types for this package. If empty, the fallback map is used."
        }),
        ("Pricing", {
            "fields": (
                "price_inr", "base_includes", "extra_price_adult_inr",
                "child_free_max_age", "child_half_max_age", "child_half_multiplier"
            ),
        }),
    )





@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user", "package", "razorpay_order_id", "razorpay_payment_id",
        "paid", "amount", "currency", "created_at",
    )
    list_filter = ("paid", "currency", "created_at")
    search_fields = ("razorpay_order_id", "razorpay_payment_id", "user__username", "user__email")


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("id", "provider", "event", "processed_ok", "matched_order", "created_at")
    list_filter = ("provider", "processed_ok", "event", "created_at")
    search_fields = ("event", "signature", "delivery_id", "matched_order__razorpay_order_id")


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    search_fields = ("name",)


@admin.register(UnitType)
class UnitTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code")
    search_fields = ("name", "code")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("name", "year", "start_date", "end_date", "active", "booking_open")
    list_filter = ("active", "booking_open", "year")
    search_fields = ("name", "location", "description")


@admin.register(EventDay)
class EventDayAdmin(admin.ModelAdmin):
    list_display = ("event", "order", "date", "title")
    list_filter = ("event",)
    search_fields = ("title", "subtitle", "description")


@admin.register(PromoCode)  # ADD
class PromoCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "kind", "value", "is_active", "start_date", "end_date")
    list_filter  = ("kind", "is_active")
    search_fields = ("code", "description")
    fieldsets = (
        (None, {"fields": ("code", "description")}),
        ("Discount", {"fields": ("kind", "value")}),
        ("Availability", {"fields": ("is_active", "start_date", "end_date")}),
    )
    def get_readonly_fields(self, request, obj=None):
        # keep fully editable; adjust if you want code immutable after creation
        return ()
    


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    # ------- List page -------
    list_display = (
        "id", "user", "event", "property", "unit_type", "category",
        "guests",
         "sightseeing_opt_in", "sightseeing_opt_in_pending", "sightseeing_requested_count",
        "primary_gender", "primary_meal_preference",   # ← NEW
        "party_brief",                                 # ← NEW (computed)
        "alloc_brief",                                 # ← NEW (computed)
        "pricing_total_inr", "status",
        "promo_code", "promo_discount_inr",
        "created_at",
    )
    list_filter = (
        "event", "property", "unit_type", "category", "status",
        "primary_gender", "primary_meal_preference", "sightseeing_opt_in", "sightseeing_opt_in_pending",  # ← NEW
    )
    search_fields = (
        "id", "user__username", "user__email", "order__razorpay_order_id",
    )

    # Make JSON fields easier to edit (simple textarea without extra deps)
    formfield_overrides = {
        dj_models.JSONField: {"widget": Textarea(attrs={"rows": 6, "cols": 100})},
    }

    readonly_fields = (
        "pricing_total_inr", "pricing_breakdown", "promo_breakdown",
        "party_brief",   # ← NEW
        "alloc_brief",   # ← NEW
    )

    fieldsets = (
        ("Core", {"fields": ("user", "event", "order", "status")}),
        ("Inventory Slice", {"fields": ("property", "unit_type", "category")}),
        ("Dates", {"fields": ("check_in", "check_out")}),
        ("Guests", {"fields": ("guests", "guest_ages", "extra_adults", "extra_children_half", "extra_children_free")}),
        ("Party & Preferences", {  # ← NEW
            "fields": ("primary_gender","primary_age", "primary_meal_preference", "companions", "party_brief"),
            "description": "Primary + companions with genders and meals. Companions are stored as JSON.",
        }),
        ("Safety", {"fields": ("blood_group", "emergency_contact_name", "emergency_contact_phone")}),
        ("Pricing Snapshot", {"fields": ("pricing_total_inr", "pricing_breakdown")}),
        ("Promotion", {
            "fields": ("promo_code", "promo_discount_inr", "promo_breakdown"),
            "description": "Snapshot captured at order creation.",
        }),
        ("Allocation", {  # ← NEW (read-only summary)
            "fields": ("alloc_brief",),
            "description": "Units allocated to this booking.",
        }),
    )

    # Prefetch related for the allocation summary to avoid N+1
    # def get_queryset(self, request):
    #     qs = super().get_queryset(request)
    #     return qs.prefetch_related(
    #         "allocation_set__unit__property",
    #         "allocation_set__unit__unit_type"
    #     ).select_related("order", "event", "property", "unit_type", "user")
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        rel_name = Allocation._meta.get_field("booking").remote_field.get_accessor_name()
        return qs.prefetch_related(
            Prefetch(
                rel_name,
                queryset=Allocation.objects.select_related("unit__property", "unit__unit_type"),
            )
        ).select_related("order", "event", "property", "unit_type", "user")
    
    # def get_queryset(self, request):
    #     qs = super().get_queryset(request)
    #     return qs.prefetch_related(
    #         # was: 'allocation_set__unit__property'
    #         Prefetch(
    #             'allocations',  # <- use the related_name
    #             queryset=Allocation.objects.select_related('unit__property', 'unit__unit_type')
    #         )
    #     )

    # ---------- Computed columns / fields ----------

    def party_brief(self, obj):
        """
        Example: '4 ppl • Genders M2/F1/O1 • Meals VEG2/NON_VEG1/JAIN1'
        Uses primary + companions JSON.
        """
        # counts
        g_map = {"M": 0, "F": 0, "O": 0}
        m_map = {"VEG": 0, "NON_VEG": 0, "VEGAN": 0, "JAIN": 0, "OTHER": 0}

        def norm_gender(val):
            v = (val or "").strip().upper()
            return v if v in g_map else "O"

        def norm_meal(val):
            v = (val or "").strip().upper().replace("-", "").replace(" ", "")
            if v in {"VEG", "VEGETARIAN", "V"}:
                return "VEG"
            if v in {"NONVEG", "NONVEGETARIAN", "NV", "N", "EGG", "EGGETARIAN", "CHICKEN", "MEAT"}:
                return "NON_VEG"
            if v in {"VEGAN", "VG"}:
                return "VEGAN"
            if v == "JAIN":
                return "JAIN"
            return "OTHER"

        # primary
        g_map[norm_gender(getattr(obj, "primary_gender", None))] += 1
        # If you store a separate primary meal field:
        m_map[norm_meal(getattr(obj, "primary_meal_preference", None))] += 1

        # companions
        comps = getattr(obj, "companions", None) or []
        for c in comps:
            if not isinstance(c, dict):
                continue
            g_map[norm_gender(c.get("gender"))] += 1
            m_map[norm_meal(c.get("meal") or c.get("meal_preference"))] += 1

        ppl = 1 + sum(1 for c in comps if isinstance(c, dict) and (c.get("name") or "").strip())
        genders = f"M{g_map['M']}/F{g_map['F']}/O{g_map['O']}"
        # show only non-zero meal buckets to keep it compact
        meal_parts = [f"{k}{v}" for k, v in m_map.items() if v]
        meals = "/".join(meal_parts) if meal_parts else "—"
        return f"{ppl} ppl • Genders {genders} • Meals {meals}"
    party_brief.short_description = "Party (summary)"
    
    def alloc_brief(self, obj):
        """
        Example: 'Green Meadows / SWISS TENT / DT-N002 | Blue Camp / DOME TENT / DT-5'
        """
        try:
            # Find the correct reverse accessor at runtime (allocations vs allocation_set)
            rel_name = Allocation._meta.get_field("booking").remote_field.get_accessor_name()
            manager = getattr(obj, rel_name, None) or getattr(obj, "allocation_set", None)

            if manager is not None:
                allocs = list(manager.all())
            else:
                # Ultimate fallback (shouldn’t be needed if FK is set up correctly)
                allocs = list(
                    Allocation.objects.filter(booking=obj).select_related("unit__property", "unit__unit_type")
                )
        except Exception:
            return "—"

        parts = []
        for a in allocs:
            u = getattr(a, "unit", None)
            if not u:
                continue
            prop  = getattr(getattr(u, "property", None), "name", "") or "—"
            utype = getattr(getattr(u, "unit_type", None), "name", "") or "—"
            # show the same label you see on the invoice
            label = getattr(u, "label", None) or f"Unit#{getattr(u, 'id', '—')}"
            parts.append(f"{prop} / {utype} / {label}")

        return " | ".join(parts) if parts else "—"


    
    
@admin.register(Allocation)
class AllocationAdmin(admin.ModelAdmin):
    list_display = ("booking", "unit", "created_at")
    list_filter = ("unit__property", "unit__unit_type", "unit__category")


# -----------------------------------
# Helpers shared by the CSV importers
# -----------------------------------
STATUS_MAP = {
    "available": "AVAILABLE", "avail": "AVAILABLE", "a": "AVAILABLE",
    "hold": "HOLD", "h": "HOLD",
    "occupied": "OCCUPIED", "o": "OCCUPIED",
    "maintenance": "MAINTENANCE", "m": "MAINTENANCE",
    "": "AVAILABLE", None: "AVAILABLE",
}

def _norm(s): return "" if s is None else str(s).strip()
def _norm_category(s): return _norm(s).upper()
def _norm_status(s): return STATUS_MAP.get(_norm(s).lower(), "AVAILABLE")

def _safe_int(v, default=2):
    try:
        iv = int(float(v))
        return max(1, iv)
    except Exception:
        return default

def _code_from_name(name):
    parts = re.findall(r"[A-Za-z0-9]+", (name or "").upper())
    if not parts:
        return "UT"
    code = "".join(p[:1] for p in parts)[:3] or "UT"
    # ensure uniqueness
    from .models import UnitType
    base, i = code, 1
    while UnitType.objects.filter(code=code).exists():
        code = f"{base}{i}"
        i += 1
    return code

def _abbr_for_unit_type(name: str) -> str:
    n = (name or "").strip().upper()
    if "DOME" in n and "TENT" in n:
        return "DT"
    if "SWISS" in n and "TENT" in n:
        return "ST"
    if "COTTAGE" in n:
        return "CT"
    if "HUT" in n:
        return "HUT"
    parts = re.findall(r"[A-Z0-9]+", n)
    return "".join(p[:1] for p in parts)[:3] or "UT"

def _next_label_start(prop, utype, category: str, abbr: str) -> int:
    """
    For labels like ST-L001, DT-N002...
    """
    from .models import Unit
    cat_initial = (category or "X")[:1].upper()
    base = f"{abbr}-{cat_initial}"
    existing = Unit.objects.filter(
        property=prop, unit_type=utype, category=category, label__startswith=base
    ).values_list("label", flat=True)
    max_n = 0
    for lbl in existing:
        m = re.match(rf"^{re.escape(base)}(\d+)$", lbl)
        if m:
            try:
                max_n = max(max_n, int(m.group(1)))
            except ValueError:
                pass
    return max_n + 1

def _materialize_units_for_row(prop, utype, category: str,
                               quantity: int, capacity: int, features: str,
                               prune_extras: bool = False) -> tuple[int, int]:
    """
    Ensure exactly `quantity` Unit rows exist for this slice.
    Create missing with auto labels & update capacity/features.
    Optionally prune extras beyond desired quantity.
    Returns: (created_count, updated_count)
    """
    from .models import Unit
    category = (category or "").upper()
    current_qs = Unit.objects.filter(property=prop, unit_type=utype, category=category).order_by("label")
    current = list(current_qs)
    created = updated = 0

    if len(current) > quantity and prune_extras:
        extras = current[quantity:]
        Unit.objects.filter(id__in=[u.id for u in extras]).delete()
        current = current[:quantity]

    if len(current) < quantity:
        abbr = _abbr_for_unit_type(utype.name)
        start = _next_label_start(prop, utype, category, abbr)
        for i in range(start, start + (quantity - len(current))):
            label = f"{abbr}-{(category or 'X')[:1].upper()}{i:03d}"
            Unit.objects.create(
                property=prop,
                unit_type=utype,
                category=category,
                label=label,
                capacity=capacity or 1,
                features=features or "",
                status="AVAILABLE",
            )
            created += 1
        current = list(Unit.objects.filter(property=prop, unit_type=utype, category=category).order_by("label"))

    for u in current:
        ch = False
        if (u.capacity or 0) != (capacity or 1):
            u.capacity = capacity or 1; ch = True
        if (u.features or "") != (features or ""):
            u.features = features or ""; ch = True
        if ch:
            u.save(update_fields=["capacity", "features"])
            updated += 1

    return created, updated


# ---------------------------------
# 1) Detailed UNIT CSV Import (old)
# ---------------------------------
class InventoryCSVUploadForm(forms.Form):
    csv_file = forms.FileField(
        help_text="CSV headers (case-insensitive): property, unit_type, category, label, capacity, status, features"
    )
    default_property = forms.CharField(
        required=False,
        help_text="Used if 'property' column is missing/empty."
    )
    prune = forms.BooleanField(
        required=False, initial=False,
        help_text="Delete Units in these Properties that are NOT present in this file."
    )

@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ("property", "unit_type", "category", "label", "capacity", "status")
    list_filter = ("property", "unit_type", "category", "status")
    search_fields = ("label", "features")
    # Use your custom change list to show the import button(s)
    change_list_template = "admin/change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        my = [
            path("import-inventory/", self.admin_site.admin_view(self.import_inventory_view),
                 name="h2h_unit_import_inventory"),
            path("import-inventory/sample/", self.admin_site.admin_view(self.import_sample_view),
                 name="h2h_unit_import_sample"),
        ]
        return my + urls

    def import_sample_view(self, request):
        # Quote facility to keep commas intact
        sample = (
            "property,unit_type,category,label,capacity,status,features\r\n"
            "Mystic Meadow,COTTAGE,DELUXE,C1,3,available,\"Lake view\"\r\n"
            "Mystic Meadow,SWISS TENT,NORMAL,ST-01,2,occupied,\"Near stage\"\r\n"
            "Mystic Meadow,DOME TENT,NORMAL,DT-A3,2,hold,\"\"\r\n"
        )
        resp = HttpResponse(sample, content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="inventory_sample.csv"'
        return resp

    def import_inventory_view(self, request):
        from .models import Property, UnitType, Unit  # local import

        if request.method == "POST":
            form = InventoryCSVUploadForm(request.POST, request.FILES)
            if form.is_valid():
                f = form.cleaned_data["csv_file"]
                default_property = form.cleaned_data.get("default_property") or ""
                prune = bool(form.cleaned_data.get("prune"))

                try:
                    text = f.read().decode("utf-8-sig")
                except Exception:
                    text = f.read().decode("utf-8")

                reader = csv.DictReader(io.StringIO(text))
                headers = [h.strip().lower() for h in (reader.fieldnames or [])]
                if "label" not in headers:
                    messages.error(request, "CSV must include a 'label' column.")
                    return redirect("..")

                created_props = created_utypes = created_units = updated_units = 0
                touched_props = set()
                seen_labels_per_prop = {}

                try:
                    with transaction.atomic():
                        for row in reader:
                            prop_name = _norm(row.get("property") or default_property)
                            utype_name = _norm(row.get("unit_type"))
                            category = _norm_category(row.get("category"))
                            label = _norm(row.get("label"))
                            capacity = _safe_int(row.get("capacity"), default=2)
                            status = _norm_status(row.get("status"))
                            features = _norm(row.get("features"))

                            if not prop_name:
                                raise ValueError("Missing property (supply column or default_property).")
                            if not utype_name:
                                raise ValueError("Missing unit_type.")
                            if not label:
                                raise ValueError("Missing label.")

                            # Property
                            prop, p_created = Property.objects.get_or_create(
                                name=prop_name, defaults={"slug": slugify(prop_name)}
                            )
                            if p_created:
                                created_props += 1
                            touched_props.add(prop.id)
                            seen_labels_per_prop.setdefault(prop.id, set()).add(label)

                            # UnitType (case-insensitive by name)
                            utype = UnitType.objects.filter(name__iexact=utype_name).first()
                            if not utype:
                                utype = UnitType.objects.create(
                                    name=utype_name.upper(),
                                    code=_code_from_name(utype_name)
                                )
                                created_utypes += 1

                            # Unit (unique per (property, label))
                            unit, u_created = Unit.objects.get_or_create(
                                property=prop,
                                label=label,
                                defaults={
                                    "unit_type": utype,
                                    "category": category,
                                    "capacity": capacity,
                                    "features": features,
                                    "status": status,
                                }
                            )
                            if u_created:
                                created_units += 1
                            else:
                                changed = False
                                if unit.unit_type_id != utype.id:
                                    unit.unit_type = utype; changed = True
                                if (unit.category or "") != category:
                                    unit.category = category; changed = True
                                if (unit.capacity or 0) != capacity:
                                    unit.capacity = capacity; changed = True
                                if (unit.features or "") != features:
                                    unit.features = features; changed = True
                                if (unit.status or "") != status:
                                    unit.status = status; changed = True
                                if changed:
                                    unit.save()
                                    updated_units += 1

                        # Optional prune: remove Units not present in CSV for touched properties
                        if prune:
                            deleted = 0
                            for pid in touched_props:
                                labels_seen = seen_labels_per_prop.get(pid, set())
                                qs = Unit.objects.filter(property_id=pid).exclude(label__in=labels_seen)
                                cnt = qs.count()
                                if cnt:
                                    qs.delete()
                                    deleted += cnt
                            messages.warning(request, f"Pruned {deleted} units not present in this CSV.")

                    messages.success(
                        request,
                        f"Import completed: Properties +{created_props}, UnitTypes +{created_utypes}, "
                        f"Units +{created_units}/~{updated_units}."
                    )
                    return redirect("..")

                except Exception as e:
                    messages.error(request, f"Import failed: {e}")
                    return redirect("..")

        else:
            form = InventoryCSVUploadForm()

        context = dict(
            self.admin_site.each_context(request),
            title="Import inventory (CSV)",
            form=form,
            opts=self.model._meta,
            sample_url="admin:h2h_unit_import_sample",
        )
        # Reuse your single uploader template
        return render(request, "admin/inventory_rows_import.html", context)


# ------------------------------------------------
# 2) Aggregated INVENTORYROW CSV Import (your CSV)
# ------------------------------------------------
class InventoryRowsCSVForm(forms.Form):
    csv_file = forms.FileField(
        help_text="CSV headers must be exactly: PROPERTY, TYPE, CATEGORY, NO OF TENT, PEOPLE SHARE PER ROOM, facility"
    )
    materialize_units = forms.BooleanField(
        required=False,
        initial=True,
        help_text="Create/adjust individual Unit rows to match quantities (recommended)."
    )
    prune_units = forms.BooleanField(
        required=False,
        initial=False,
        help_text="If enabled, delete extra Units beyond the quantity in CSV (touched rows only)."
    )

@admin.register(InventoryRow)
class InventoryRowAdmin(admin.ModelAdmin):
    list_display = ("property", "unit_type", "category", "quantity", "capacity_per_unit", "total_capacity", "facility")
    list_filter  = ("property", "unit_type", "category")
    search_fields = ("property__name", "unit_type__name", "category", "facility")

    def get_urls(self):
        urls = super().get_urls()
        my = [
            path("import/", self.admin_site.admin_view(self.import_rows_view), name="h2h_inventoryrow_import"),
            path("sample/", self.admin_site.admin_view(self.sample_rows_view), name="h2h_inventoryrow_sample"),
        ]
        return my + urls

    def sample_rows_view(self, request):
        # QUOTED facility field to keep commas intact
        sample = (
            "PROPERTY,TYPE,CATEGORY,NO OF TENT,PEOPLE SHARE PER ROOM,facility\r\n"
            "BANJARA,DOME TENT,NORMAL,5,2,\"pillow,mattress, sharing blankets, light & charging poin, comon wash room(5 male & 5 female)\"\r\n"
            "BANJARA,SWISS TENT,LUXURY,22,4,\"IRON BED, Sharing blanketss, mini cooler, attuch bathroom\"\r\n"
            "BANJARA,SWISS TENT,B TYPE,2,6,\"IRON BED, Sharing blanketss, mini cooler, attuch bathroom\"\r\n"
            "BANJARA,SWISS TENT,C TYPE,9,4,\"IRON BED, Sharing blanketss, mini cooler, attuch bathroom\"\r\n"
        )
        resp = HttpResponse(sample, content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="inventory_rows_sample.csv"'
        return resp

    def import_rows_view(self, request):
        from .models import Property, UnitType, InventoryRow  # local import

        if request.method == "POST":
            form = InventoryRowsCSVForm(request.POST, request.FILES)
            if form.is_valid():
                f = form.cleaned_data["csv_file"]
                materialize = bool(form.cleaned_data.get("materialize_units"))
                prune = bool(form.cleaned_data.get("prune_units"))

                raw = f.read()
                try:
                    text = raw.decode("utf-8-sig")
                except Exception:
                    text = raw.decode("utf-8")

                # Let csv sniff delimiter; fall back to comma
                try:
                    dialect = csv.Sniffer().sniff(text.splitlines()[0])
                    delim = dialect.delimiter
                except Exception:
                    delim = ","

                reader = csv.DictReader(io.StringIO(text), delimiter=delim)
                headers = [h.strip() for h in (reader.fieldnames or [])]

                required = ["PROPERTY", "TYPE", "CATEGORY", "NO OF TENT", "PEOPLE SHARE PER ROOM", "facility"]
                missing = [h for h in required if h not in headers]
                if missing:
                    messages.error(request, f"Missing required column(s): {', '.join(missing)}")
                    return redirect("..")

                created_rows = updated_rows = 0
                created_units = updated_units = 0

                try:
                    with transaction.atomic():
                        for row in reader:
                            prop_name = (row.get("PROPERTY") or "").strip()
                            utype_name = (row.get("TYPE") or "").strip()
                            category   = (row.get("CATEGORY") or "").strip()
                            qty_raw    = (row.get("NO OF TENT") or "0").strip()
                            cap_raw    = (row.get("PEOPLE SHARE PER ROOM") or "1").strip()
                            facility   = (row.get("facility") or "").strip()

                            try:
                                quantity = max(0, int(float(qty_raw)))
                            except Exception:
                                quantity = 0
                            try:
                                capacity = max(1, int(float(cap_raw)))
                            except Exception:
                                capacity = 1

                            if not prop_name or not utype_name:
                                raise ValueError("PROPERTY and TYPE are required in every row.")

                            prop, _ = Property.objects.get_or_create(
                                name=prop_name,
                                defaults={"slug": slugify(prop_name)}
                            )
                            utype = UnitType.objects.filter(name__iexact=utype_name).first()
                            if not utype:
                                utype = UnitType.objects.create(
                                    name=utype_name,
                                    code=_code_from_name(utype_name),
                                )

                            ir, was_created = InventoryRow.objects.get_or_create(
                                property=prop,
                                unit_type=utype,
                                category=category,
                                defaults={
                                    "quantity": quantity,
                                    "capacity_per_unit": capacity,
                                    "facility": facility,
                                }
                            )
                            if was_created:
                                created_rows += 1
                            else:
                                ch = False
                                if ir.quantity != quantity:
                                    ir.quantity = quantity; ch = True
                                if ir.capacity_per_unit != capacity:
                                    ir.capacity_per_unit = capacity; ch = True
                                if (ir.facility or "") != facility:
                                    ir.facility = facility; ch = True
                                if ch:
                                    ir.save()
                                    updated_rows += 1

                            if materialize:
                                c, u = _materialize_units_for_row(
                                    prop=prop, utype=utype, category=category,
                                    quantity=quantity, capacity=capacity, features=facility,
                                    prune_extras=prune,
                                )
                                created_units += c
                                updated_units += u

                    msg = f"Imported rows: +{created_rows}/~{updated_rows}."
                    if materialize:
                        msg += f" Units created: {created_units}, updated: {updated_units}"
                        if prune:
                            msg += " (pruned extras)."
                    messages.success(request, msg)
                    return redirect("..")

                except Exception as e:
                    messages.error(request, f"Import failed: {e}")
                    return redirect("..")

        else:
            form = InventoryRowsCSVForm()

        context = dict(
            self.admin_site.each_context(request),
            title="Import inventory rows (CSV)",
            form=form,
            opts=self.model._meta,
            sample_url="admin:h2h_inventoryrow_sample",
        )
        return render(request, "admin/inventory_rows_import.html", context)




@admin.register(SightseeingRegistration)
class SightseeingRegistrationAdmin(admin.ModelAdmin):
    list_display = ("id", "booking", "user", "guests", "status", "pay_at_venue", "created_at")
    list_filter = ("status", "pay_at_venue")
    search_fields = ("booking__id", "user__username", "user__email")
