from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.mixins import BaseTenantViewSet

from .models import Contact, ContactRole
from .serializers import (
    ContactRoleSerializer,
    ContactSerializer,
    MergeSerializer,
)
from .services import find_duplicates, merge_contacts


class ContactViewSet(BaseTenantViewSet):
    queryset = Contact.objects.all().order_by("-created_at")
    serializer_class = ContactSerializer
    search_fields = ["full_name", "company_name", "email", "phone"]
    filterset_fields = ["kind", "is_vip", "assigned_agent", "source"]
    # Row-level scope: agents see their own contacts; managers their branch.
    scope_owner_field = "assigned_agent"
    scope_branch_field = "assigned_agent__branch"

    @action(detail=False, methods=["get"])
    def duplicates(self, request):
        """Preview likely duplicates for given identifiers (dedupe at capture)."""
        qs = find_duplicates(
            email=request.query_params.get("email", ""),
            phone=request.query_params.get("phone", ""),
            name=request.query_params.get("name", ""),
        )
        return Response(ContactSerializer(qs, many=True).data)

    @action(detail=True, methods=["post"])
    def merge(self, request, pk=None):
        """Merge a duplicate contact into this survivor."""
        survivor = self.get_object()
        ser = MergeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        duplicate = get_object_or_404(
            Contact.objects, pk=ser.validated_data["duplicate_id"]
        )
        merged = merge_contacts(survivor=survivor, duplicate=duplicate)
        return Response(ContactSerializer(merged).data)

    @action(detail=True, methods=["get", "post"])
    def roles(self, request, pk=None):
        contact = self.get_object()
        if request.method == "GET":
            return Response(
                ContactRoleSerializer(contact.contact_roles.all(), many=True).data
            )
        ser = ContactRoleSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        role, _ = ContactRole.objects.update_or_create(
            tenant_id=contact.tenant_id,
            contact=contact,
            role=ser.validated_data["role"],
            defaults={"is_active": ser.validated_data.get("is_active", True)},
        )
        return Response(ContactRoleSerializer(role).data, status=status.HTTP_201_CREATED)
