"""
Test settings.

Always run against the LOCAL Postgres (Docker), never the Neon `DATABASE_URL` from .env —
tests create/drop a test database and must never touch the deployed data. Postgres (not
SQLite) is required because the tenancy suite exercises real Row-Level Security, which is a
Postgres-only feature.
"""
from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# Force the local Postgres regardless of any DATABASE_URL in the environment/.env.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "crm"),
        "USER": env("POSTGRES_USER", "crm"),
        "PASSWORD": env("POSTGRES_PASSWORD", "crm"),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "5432"),
    }
}

# Faster hashing in tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
