from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from .admin_api import router
from django.db import models

@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_config_view(request):
    """
    Returns configuration for the mobile admin app.
    Introspects the registered ViewSets in admin_api.py.
    """
    config = {
        "models": []
    }

    # First pass: Build a map of Model -> URL Key
    model_to_key = {}
    for prefix, viewset, basename in router.registry:
        model_cls = getattr(viewset, "queryset", None)
        if hasattr(model_cls, "model"):
            model_cls = model_cls.model
        if model_cls:
            model_to_key[model_cls] = prefix

    for prefix, viewset, basename in router.registry:
        model_cls = getattr(viewset, "queryset", None)
        if hasattr(model_cls, "model"): # Handle QuerySet
            model_cls = model_cls.model
        
        if not model_cls:
            continue

        # Basic Info
        model_name = model_cls._meta.object_name
        app_label = model_cls._meta.app_label
        verbose_name_plural = str(model_cls._meta.verbose_name_plural)
        
        # Fields Inspection
        fields_config = []
        filter_fields = getattr(viewset, "filterset_fields", [])
        search_fields = getattr(viewset, "search_fields", [])
        ordering_fields = getattr(viewset, "ordering_fields", ["id"])
        
        for field in model_cls._meta.fields:
            field_type = "text"
            if isinstance(field, models.AutoField): field_type = "number" # ID
            elif isinstance(field, models.IntegerField): field_type = "number"
            elif isinstance(field, models.CharField): field_type = "text"
            elif isinstance(field, models.TextField): field_type = "text"
            elif isinstance(field, models.BooleanField): field_type = "boolean"
            elif isinstance(field, (models.DateField, models.DateTimeField)): field_type = "date"
            elif isinstance(field, (models.ImageField, models.FileField)): field_type = "file"
            elif isinstance(field, models.ForeignKey): field_type = "fk"
            
            # TODO: Choices
            choices = []
            if field.choices:
                field_type = "choice"
                choices = [{"label": c[1], "value": c[0]} for c in field.choices]

            fields_config.append({
                "name": field.name,
                "label": str(field.verbose_name).title(),
                "type": field_type,
                "required": not field.blank,
                "read_only": not field.editable,
                "choices": choices,
                "is_filter": field.name in filter_fields,
                "is_search": field.name in search_fields or any(s.startswith(field.name + "__") for s in search_fields),
                "related_model": field.related_model._meta.label if field.related_model else None,
                "related_key": model_to_key.get(field.related_model) if field.related_model else None, # key for fetching options
            })

        # Introspect Serializer for computed fields (e.g. party_brief)
        serializer_cls = getattr(viewset, "serializer_class", None)
        if serializer_cls:
            try:
                # We need a context-less instance to inspect static fields
                ser_instance = serializer_cls()
                for ser_field_name, ser_field_obj in ser_instance.get_fields().items():
                    # Skip if already added via model loop
                    if any(f["name"] == ser_field_name for f in fields_config):
                        continue
                    
                    # Inspect type
                    # SerializerMethodField -> text
                    # ReadOnlyField -> text
                    from rest_framework import serializers
                    f_type = "text"
                    if isinstance(ser_field_obj, (serializers.IntegerField, serializers.DecimalField)):
                        f_type = "number"
                    elif isinstance(ser_field_obj, serializers.BooleanField):
                        f_type = "boolean"
                    
                    # We treat all non-model serializer fields as read_only computed columns
                    fields_config.append({
                        "name": ser_field_name,
                        "label": ser_field_name.replace("_", " ").title(),
                        "type": f_type,
                        "required": False,
                        "read_only": True,
                        "choices": [],
                        "is_filter": False,
                        "is_search": False,
                        "related_model": None,
                        "related_key": None
                    })
            except Exception as e:
                pass # Fallback if serializer inspection fails

        # Actions (Bulk)
        actions = ["delete"] # default
        # TODO: Add custom actions inspection

        config["models"].append({
            "key": prefix, # URL component e.g. 'bookings'
            "label": verbose_name_plural.title(),
            "model_name": model_name,
            "fields": fields_config,
            "actions": actions,
            "search_enabled": bool(search_fields),
        })

    return Response(config)
