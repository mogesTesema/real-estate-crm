"""Integration adapters (plan §2.5)."""
from django.conf import settings
from django.utils.module_loading import import_string


def get_sms_provider():
    return import_string(settings.SMS_PROVIDER)()


def get_email_provider():
    return import_string(settings.EMAIL_PROVIDER)()
