from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"

    def ready(self):
        from .audit import register_audit
        from .models import Branch, Company, FieldDefinition, Team

        for model in (Company, Branch, Team, FieldDefinition):
            register_audit(model)
