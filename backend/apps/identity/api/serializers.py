"""Serializers for the identity API.

Note on shape: a user's roles are a **list**, not a scalar. `identity_user_role` is a through
table with no `is_primary` flag, so "the user's role" is genuinely undefined when several are
attached. The pre-v3.2 API returned a single `role` string; consumers need updating.
"""
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .. import services
from ..models import Branch, Company, PortalProfile, Role, Team, User
from ..services import STAFF_ROLE_CODES


class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ["id", "code", "name", "description", "data_scope", "is_system_role"]
        read_only_fields = fields


class BranchBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ["id", "name", "code"]
        read_only_fields = fields


class TeamBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Team
        fields = ["id", "name", "code"]
        read_only_fields = fields


class CompanyBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = ["id", "name"]
        read_only_fields = fields


class UserSummarySerializer(serializers.ModelSerializer):
    """The directory view: enough to recognise a colleague and pick them as an assignee.

    Used for requesters whose broadest scope is OWN or MANAGED_PROPERTIES. Row breadth and
    column breadth are separate decisions — such a requester can see *who* works in their
    branch without seeing their phone number, employee number or role list.
    """

    full_name = serializers.CharField(read_only=True)
    branch = BranchBriefSerializer(read_only=True)
    team = TeamBriefSerializer(read_only=True)

    class Meta:
        model = User
        fields = ["id", "full_name", "job_title", "branch", "team", "is_active"]
        read_only_fields = fields


class UserSerializer(serializers.ModelSerializer):
    """The full view, for requesters with branch-wide scope or broader."""

    full_name = serializers.CharField(read_only=True)
    branch = BranchBriefSerializer(read_only=True)
    team = TeamBriefSerializer(read_only=True)
    roles = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "phone",
            "employee_number",
            "job_title",
            "branch",
            "team",
            "roles",
            "is_active",
            "must_change_password",
            "last_login",
            "created_at",
        ]
        read_only_fields = fields

    def get_roles(self, obj) -> list[dict]:
        return RoleSerializer(
            [link.role for link in obj.user_roles.all()], many=True
        ).data


class UserUpdateSerializer(serializers.ModelSerializer):
    """Profile edits. Role, password and activation are changed through their own routes,
    so that each goes through the service function that authorises it."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "phone", "employee_number", "job_title"]


class MeSerializer(UserSerializer):
    """The session contract: who am I, and what may I do?"""

    company = serializers.SerializerMethodField()
    data_scopes = serializers.SerializerMethodField()
    grantable_role_codes = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + [
            "company",
            "data_scopes",
            "grantable_role_codes",
            "is_superuser",
        ]
        read_only_fields = fields

    def get_company(self, obj) -> dict | None:
        # There is no User.company FK; company is reached through the branch, which is
        # nullable — an unplaced user (a fresh superuser) legitimately has none.
        if obj.branch_id is None:
            return None
        return CompanyBriefSerializer(obj.branch.company).data

    def get_data_scopes(self, obj) -> list[str]:
        return sorted({link.role.data_scope for link in obj.user_roles.all()})

    def get_grantable_role_codes(self, obj) -> list[str]:
        # Lets the frontend render only the roles this user may actually create.
        from ..services import grantable_role_codes

        return sorted(grantable_role_codes(obj))


class RegistrationSerializer(serializers.Serializer):
    """Input for registering a staff member.

    Field-level validation only. *Authorisation* — may this actor grant this role, into this
    branch — belongs to `services.register_user` so it cannot be bypassed.
    """

    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    role_code = serializers.CharField(max_length=50)
    branch = serializers.PrimaryKeyRelatedField(
        queryset=Branch.objects.all(), required=False, allow_null=True
    )
    team = serializers.PrimaryKeyRelatedField(
        queryset=Team.objects.all(), required=False, allow_null=True
    )
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    employee_number = serializers.CharField(
        max_length=50, required=False, allow_blank=True
    )
    job_title = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate_role_code(self, value):
        # A clear message for the one role this endpoint structurally cannot grant, rather
        # than the generic "your role does not permit" that services would otherwise raise.
        if value == Role.PORTAL:
            from ..services import PORTAL_REFUSAL

            raise serializers.ValidationError(PORTAL_REFUSAL)
        if value not in STAFF_ROLE_CODES:
            raise serializers.ValidationError(
                f"Unknown role '{value}'. Expected one of: "
                f"{', '.join(sorted(STAFF_ROLE_CODES))}."
            )
        return value


class AssignRoleSerializer(serializers.Serializer):
    role_code = serializers.CharField(max_length=50)


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )
    new_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )


# --- Authentication lifecycle ---------------------------------------------------------------


class LoginSerializer(TokenObtainPairSerializer):
    """JWT login that also enforces portal eligibility.

    A portal client's right to be here expires with their contract (SRS 3.11.2), so the check
    belongs at every login rather than only at invitation. Stashes the authenticated user on
    the request so the view can audit the success without re-querying.
    """

    def validate(self, attrs):
        data = super().validate(attrs)

        blocked = services.portal_login_blocked_reason(self.user)
        if blocked:
            raise AuthenticationFailed(blocked, code="portal_access_inactive")

        request = self.context.get("request")
        if request is not None:
            request._authenticated_user = self.user
        return data


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(write_only=True)


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )


# --- Portal ----------------------------------------------------------------------------------


class PortalProfileSerializer(serializers.ModelSerializer):
    """A portal client as staff see them."""

    email = serializers.EmailField(source="user.email", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    is_active = serializers.BooleanField(source="user.is_active", read_only=True)
    must_change_password = serializers.BooleanField(
        source="user.must_change_password", read_only=True
    )

    class Meta:
        model = PortalProfile
        fields = [
            "id",
            "email",
            "full_name",
            "contact",
            "portal_type",
            "eligibility_status",
            "completed_contract_ref_type",
            "completed_contract_ref_id",
            "is_verified",
            "last_access_at",
            "is_active",
            "must_change_password",
        ]
        read_only_fields = fields


class PortalAccessSerializer(serializers.Serializer):
    """Input for inviting a client to the portal.

    Deliberately takes the *contract* rather than a name and email: the client's identity is
    read from the contact by whichever app owns the contract, so staff cannot invent a login
    for someone who has no contract (SRS 3.11.2).
    """

    contact_id = serializers.UUIDField()
    portal_type = serializers.ChoiceField(choices=PortalProfile.PortalType.choices)
    contract_ref_type = serializers.ChoiceField(
        choices=PortalProfile.ContractRefType.choices
    )
    contract_ref_id = serializers.UUIDField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate(self, attrs):
        # A buyer is not established by a lease, nor a tenant by a sale. Catching the
        # mismatch here gives a clear message instead of a bare "no contract confirms this".
        expected = {
            "BUYER": "TRANSACTION",
            "SELLER": "TRANSACTION",
            "TENANT": "LEASE",
            "LANDLORD": "LEASE",
        }[attrs["portal_type"]]
        if attrs["contract_ref_type"] != expected:
            raise serializers.ValidationError(
                {
                    "contract_ref_type": (
                        f"A {attrs['portal_type'].lower()} portal is established by a "
                        f"{expected.lower()}, not a {attrs['contract_ref_type'].lower()}."
                    )
                }
            )
        return attrs
