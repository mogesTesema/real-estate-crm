from django.apps import AppConfig


class DealsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.deals"
    label = "deals"

    def ready(self):
        from apps.core.audit import register_audit

        from .models import Opportunity

        register_audit(Opportunity)
