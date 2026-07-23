from django.apps import AppConfig


class PropertiesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.properties"
    label = "properties"

    def ready(self):
        from apps.core.audit import register_audit

        from .models import Listing, Property

        register_audit(Property)
        register_audit(Listing)
