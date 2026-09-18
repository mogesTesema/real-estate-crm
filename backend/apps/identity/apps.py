from django.apps import AppConfig


class IdentityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.identity"
    label = "identity"

    def ready(self):
        """Register this app's row-scoping rules (architecture.md §2).

        Every app does this from `ready()` rather than at module import: the scope tables name
        model fields, so they must not run before the app registry is populated.
        """
        from .selectors import register_resources

        register_resources()
