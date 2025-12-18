from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status
from django.forms.models import model_to_dict
from .models import AuditLog

class AuditLogMixin:
    """
    Automatic audit logging for Create, Update, Delete.
    """
    def _log(self, request, action, obj, changes=None):
        if not request.user.is_authenticated:
            return
            
        AuditLog.objects.create(
            actor=request.user,
            action=action,
            model_name=obj._meta.label,
            object_id=str(obj.pk),
            object_repr=str(obj),
            changes=changes or {},
            remote_addr=request.META.get('REMOTE_ADDR')
        )

    def perform_create(self, serializer):
        obj = serializer.save()
        self._log(self.request, "CREATE", obj, changes=serializer.data)

    def perform_update(self, serializer):
        # Calculate diff? For now just log new data
        obj = serializer.save()
        self._log(self.request, "UPDATE", obj, changes=serializer.data)

    def perform_destroy(self, instance):
        self._log(self.request, "DELETE", instance)
        instance.delete()


class BulkActionMixin:
    """
    Adds /bulk/ endpoint for mass actions.
    """
    @action(detail=False, methods=['post'], url_path='bulk')
    def bulk_action(self, request):
        """
        Payload: {
            "ids": [1, 2, 3],
            "action": "delete" | "status_update",
            "payload": {"status": "CONFIRMED"}
        }
        """
        ids = request.data.get('ids', [])
        action = request.data.get('action')
        payload = request.data.get('payload', {})
        
        if not ids or not action:
            return Response({"error": "Missing ids or action"}, status=400)

        queryset = self.filter_queryset(self.get_queryset()).filter(id__in=ids)
        count = queryset.count()
        
        if action == "delete":
            # Log deletions
            for obj in queryset:
                AuditLog.objects.create(
                    actor=request.user, 
                    action="DELETE", 
                    model_name=obj._meta.label,
                    object_id=str(obj.pk),
                    object_repr=str(obj),
                    changes={"bulk": True}
                )
            queryset.delete()
            return Response({"message": f"Deleted {count} items"})
            
        elif action == "update":
            # Generic update
            updated = queryset.update(**payload)
            # Log bulk update (simplified)
            AuditLog.objects.create(
                 actor=request.user,
                 action="BULK",
                 model_name=queryset.model._meta.label,
                 object_id="MULTIPLE",
                 changes={"ids": ids, "update": payload}
            )
            return Response({"message": f"Updated {updated} items"})
            
        return Response({"error": "Unknown action"}, status=400)
