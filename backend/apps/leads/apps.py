from django.apps import AppConfig


class LeadsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.leads"
    label = "leads"

    def ready(self):
        from apps.core.audit import register_audit

        from .models import Lead

        register_audit(Lead)
