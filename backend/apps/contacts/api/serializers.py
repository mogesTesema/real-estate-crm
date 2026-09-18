"""Serializers for the contacts API.

Write serializers validate shape only. Every mutation is delegated to `contacts.services`, so
the same rules hold from a shell, a management command or an import — a rule enforced by the
services module, not by convention here.
"""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.identity.models import User

from ..models import Consent, Contact, ContactRelationship, ContactRole
from ..services import IMPORTABLE_FIELDS


class AgentSummarySerializer(serializers.ModelSerializer):
    """The assigned agent, as much of them as a contact record needs.

    Defined here rather than imported from `identity.api.serializers`: §1.2 makes every app's
    `api` package private, and `lint-imports` enforces it. Duplicating four field names is the
    price of that boundary, and a cheap one — the alternative is every app reaching into
    every other app's HTTP layer.
    """

    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ("id", "full_name", "email")


class ContactRoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactRole
        fields = ("id", "role")


class ConsentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Consent
        fields = (
            "id",
            "channel",
            "status",
            "source",
            "consented_at",
            "withdrawn_at",
            "evidence",
        )
        read_only_fields = ("id", "consented_at", "withdrawn_at")


class RelationshipSerializer(serializers.ModelSerializer):
    to_contact_name = serializers.CharField(source="to_contact.__str__", read_only=True)

    class Meta:
        model = ContactRelationship
        fields = (
            "id",
            "from_contact",
            "to_contact",
            "to_contact_name",
            "relationship_type",
            "notes",
            "created_at",
        )
        read_only_fields = ("id", "created_at", "from_contact")


class ContactSummarySerializer(serializers.ModelSerializer):
    """The shape other modules embed — a lead's contact, a deal's primary contact."""

    display_name = serializers.CharField(source="__str__", read_only=True)

    class Meta:
        model = Contact
        fields = ("id", "display_name", "contact_type", "email", "phone")


class ContactSerializer(serializers.ModelSerializer):
    display_name = serializers.CharField(source="__str__", read_only=True)
    roles = serializers.SerializerMethodField()
    assigned_agent = AgentSummarySerializer(read_only=True)
    consents = ConsentSerializer(many=True, read_only=True)

    class Meta:
        model = Contact
        fields = (
            "id",
            "display_name",
            "contact_type",
            "first_name",
            "middle_name",
            "last_name",
            "company_name",
            "email",
            "secondary_email",
            "phone",
            "secondary_phone",
            "date_of_birth",
            "national_id",
            "tax_number",
            "preferred_language",
            "preferred_contact_method",
            "address_line_1",
            "address_line_2",
            "city",
            "state",
            "country",
            "postal_code",
            "latitude",
            "longitude",
            "assigned_agent",
            "default_source",
            "notes",
            "custom_data",
            "is_active",
            "roles",
            "consents",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_roles(self, obj):
        # Iterating the prefetched set rather than .values_list(), which would issue a query
        # per contact and undo the prefetch on a 25-row page.
        return sorted(role.role for role in obj.roles.all())


class ContactWriteSerializer(serializers.ModelSerializer):
    """Create / update payload.

    `email` and `phone` are plain CharFields rather than the model's EmailField: normalisation
    and the decision to accept a messy legacy value belong to `contacts.services`, and a
    serializer-level format check here would reject rows the import is required to keep.
    """

    roles = serializers.ListField(
        child=serializers.ChoiceField(choices=ContactRole.Role.choices),
        required=False,
        help_text="The hats this contact wears. A set, not one value (SRS 3.2.2).",
    )
    email = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    secondary_email = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    # Optional with no default, rather than defaulting to PERSON here. A default would be
    # re-injected on every PATCH, silently flipping a COMPANY back to PERSON whenever the
    # field was omitted. Create falls back to PERSON inside the service; update keeps what
    # the record already has.
    contact_type = serializers.ChoiceField(choices=Contact.ContactType.choices, required=False)

    class Meta:
        model = Contact
        fields = (
            "contact_type",
            "first_name",
            "middle_name",
            "last_name",
            "company_name",
            "email",
            "secondary_email",
            "phone",
            "secondary_phone",
            "date_of_birth",
            "national_id",
            "tax_number",
            "preferred_language",
            "preferred_contact_method",
            "address_line_1",
            "address_line_2",
            "city",
            "state",
            "country",
            "postal_code",
            "latitude",
            "longitude",
            "assigned_agent",
            "default_source",
            "notes",
            "custom_data",
            "is_active",
            "roles",
        )


class SetRolesSerializer(serializers.Serializer):
    roles = serializers.ListField(child=serializers.ChoiceField(choices=ContactRole.Role.choices))


class MergeSerializer(serializers.Serializer):
    """`duplicate_id` is folded into the contact addressed by the URL."""

    duplicate_id = serializers.UUIDField()


class DuplicateQuerySerializer(serializers.Serializer):
    email = serializers.CharField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True)
    national_id = serializers.CharField(required=False, allow_blank=True)
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True)
    company_name = serializers.CharField(required=False, allow_blank=True)
    exclude_id = serializers.UUIDField(required=False)

    def validate(self, attrs):
        if not any(v for v in attrs.values()):
            raise serializers.ValidationError(
                "Pass at least one of email, phone, national_id or a name to match on."
            )
        return attrs


class DuplicateMatchSerializer(serializers.Serializer):
    """Documentation of the shape `selectors.find_duplicates` returns.

    Half the fields are conditional: a match the caller may not see reports its existence and
    who owns it, never its detail. See the note on `selectors._match`.
    """

    id = serializers.UUIDField(allow_null=True)
    display_name = serializers.CharField()
    matched_on = serializers.CharField()
    score = serializers.FloatField()
    in_scope = serializers.BooleanField()
    email = serializers.CharField(required=False, allow_null=True)
    phone = serializers.CharField(required=False, allow_null=True)
    owned_by = serializers.CharField(required=False, allow_null=True)
    hint = serializers.CharField(required=False)


class ImportSerializer(serializers.Serializer):
    """Upload → mapping → dry-run → commit (SRS 3.2.6).

    `commit` defaults to false so the default outcome of posting a file is a report, never a
    write. Getting that the wrong way round means a mis-mapped column is discovered after
    five thousand rows have landed.
    """

    file = serializers.FileField()
    mapping = serializers.DictField(
        child=serializers.CharField(),
        required=False,
        help_text=(
            "target field -> CSV column header. Omit to use the suggested mapping. "
            f"Target fields: {', '.join(IMPORTABLE_FIELDS)}"
        ),
    )
    roles = serializers.ListField(
        child=serializers.ChoiceField(choices=ContactRole.Role.choices), required=False
    )
    commit = serializers.BooleanField(default=False)
