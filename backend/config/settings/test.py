"""
Test settings.

Always run against the LOCAL Postgres (Docker), never the Neon `DATABASE_URL` from .env —
tests create/drop a test database and must never touch the deployed data. Postgres (with
PostGIS) is required, not SQLite: GeoDjango geography columns and the append-only
REVOKE-based immutability migrations are both Postgres-only features.
"""
from .base import *  # noqa: F401,F403
from .base import _GIS_ENGINE, env

DEBUG = False

# Force the local Postgres regardless of any DATABASE_URL in the environment/.env.
DATABASES = {
    "default": {
        "ENGINE": _GIS_ENGINE,
        "NAME": env("POSTGRES_DB", "crm"),
        "USER": env("POSTGRES_USER", "crm"),
        "PASSWORD": env("POSTGRES_PASSWORD", "crm"),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "5432"),
    }
}

# Faster hashing in tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
