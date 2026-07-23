from apps.core.mixins import BaseTenantViewSet

from .models import Activity
from .serializers import ActivitySerializer


class ActivityViewSet(BaseTenantViewSet):
    queryset = Activity.objects.all().order_by("-at")
    serializer_class = ActivitySerializer
    filterset_fields = ["activity_type", "actor", "content_type", "object_id"]
    # Timeline is visible to anyone in the tenant who can see the parent record.
    scope_owner_field = None

    def perform_create(self, serializer):
        serializer.save(
            tenant_id=self.request.user.tenant_id, actor=self.request.user
        )
