from django.apps import AppConfig


class PlatformConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.platform"
    label = "platform"

    def ready(self):
        """Connect this app's receivers.

        They answer questions `identity` cannot answer for itself: it may not import this app
        (architecture.md §1.2), so the dependency is inverted and this app registers with it.
        """
        from . import receivers
        from .scoping import register_resources

        receivers.connect()
        register_resources()
