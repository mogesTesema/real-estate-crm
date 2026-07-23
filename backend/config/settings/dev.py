"""Development settings."""
from .base import *  # noqa: F401,F403
from .base import env_bool

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Verbose SQL logging can be toggled on when debugging tenancy/RLS.
if env_bool("SQL_DEBUG", False):
    LOGGING = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": {"class": "logging.StreamHandler"}},
        "loggers": {
            "django.db.backends": {"handlers": ["console"], "level": "DEBUG"},
        },
    }
