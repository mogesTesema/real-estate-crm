"""Django admin for identity.

Deliberately minimal, and present for one reason: **bootstrap**. This pass ships no
branches/teams API, so the first Company and Branch have to be creatable somehow before
anyone can be registered into them. `manage.py createsuperuser` gets you an account (and,
since `UserManager.create_superuser` attaches the role, a usable one); admin gets you the org
structure to place people in.

Day-to-day user administration belongs to /api/v1/users/, which enforces the registration
authority matrix. Admin bypasses that entirely — it is a superuser tool, not a second door
into the same workflow.
"""
from django.contrib import admin

from .models import Branch, Company, Role, Team, User, UserRole


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ["name", "country", "default_currency", "fx_mode"]
    search_fields = ["name", "legal_name"]


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "company", "is_active"]
    list_filter = ["is_active", "company"]
    search_fields = ["name", "code"]


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "branch", "is_active"]
    list_filter = ["is_active", "branch"]
    search_fields = ["name", "code"]


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "data_scope", "is_system_role"]
    list_filter = ["data_scope", "is_system_role"]
    search_fields = ["code", "name"]  # required by UserRoleInline.autocomplete_fields
    readonly_fields = ["code"]  # role codes are referenced by application logic


class UserRoleInline(admin.TabularInline):
    model = UserRole
    extra = 0
    autocomplete_fields = ["role"]


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ["email", "full_name", "branch", "team", "is_active", "is_superuser"]
    list_filter = ["is_active", "is_staff", "is_superuser", "branch"]
    search_fields = ["email", "first_name", "last_name"]
    inlines = [UserRoleInline]
    # `password` is a hash; editing it as text would silently lock the account out.
    exclude = ["password", "user_permissions", "groups"]
    readonly_fields = ["last_login", "created_at", "updated_at"]
