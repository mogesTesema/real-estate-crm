from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import (
    AuditLog,
    Branch,
    Company,
    FieldDefinition,
    Notification,
    Team,
    Tenant,
    User,
)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "full_name", "role", "tenant", "is_active")
    search_fields = ("email", "full_name")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "role", "tenant", "branch", "team")}),
        ("Flags", {"fields": ("is_active", "is_staff", "is_superuser", "mfa_enabled")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "role", "tenant"),
            },
        ),
    )


admin.site.register(Tenant)
admin.site.register(Company)
admin.site.register(Branch)
admin.site.register(Team)
admin.site.register(FieldDefinition)
admin.site.register(AuditLog)
admin.site.register(Notification)
