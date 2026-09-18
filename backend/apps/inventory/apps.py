from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inventory"
    label = "inventory"

    def ready(self):
        """Register this app's row-scoping rules (architecture.md §2)."""
        from .scoping import register_resources

        register_resources()
