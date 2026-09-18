from django.apps import AppConfig


class ContactsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.contacts"
    label = "contacts"

    def ready(self):
        """Register this app's row-scoping rules (architecture.md §2)."""
        from .scoping import register_resources

        register_resources()
