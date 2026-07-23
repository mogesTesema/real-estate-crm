from django.apps import AppConfig


class ContactsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.contacts"
    label = "contacts"

    def ready(self):
        from apps.core.audit import register_audit

        from .models import Contact, ContactRole

        register_audit(Contact)
        register_audit(ContactRole)
