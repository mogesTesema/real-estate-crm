from django.apps import AppConfig


class PropertyOpsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.property_ops"
    label = "property_ops"

    def ready(self):
        """Connect this app's receivers.

        They answer questions `identity` cannot answer for itself: it may not import this app
        (architecture.md §1.2), so the dependency is inverted and this app registers with it.
        """
        from . import receivers

        receivers.connect()
