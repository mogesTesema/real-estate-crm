"""
Base Django settings for the Real Estate CRM.

Shared across dev/prod. Environment-specific overrides live in dev.py / prod.py.
See implimentation-plan.md §1 for the stack rationale.
"""
import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

# backend/ directory (two levels up from this file: config/settings/base.py)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

load_dotenv(BASE_DIR / ".env")


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    return env(key, str(default)).lower() in ("1", "true", "yes", "on")


SECRET_KEY = env("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0").split(",")

# --- Applications -----------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "drf_spectacular",
    "django_filters",
    "corsheaders",
]

LOCAL_APPS = [
    "apps.core",
    "apps.contacts",
    "apps.properties",
    "apps.leads",
    "apps.deals",
    "apps.activities",
    "apps.integrations",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# --- Middleware -------------------------------------------------------------
# TenantMiddleware runs AFTER authentication so it can read the tenant from the
# authenticated user / JWT and bind it to the DB connection for RLS.
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.TenantMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- Database ---------------------------------------------------------------
# DATABASE_URL (Neon/Render) wins when present; otherwise fall back to POSTGRES_* parts.
# RLS uses a session-scoped GUC (apps/core/tenancy.py), which needs a real per-connection
# session. Neon's `-pooler` endpoint is a transaction pooler that breaks session GUCs, so
# we rewrite it to the direct host. Direct connections are the right choice for a
# persistent gunicorn server anyway.
_DATABASE_URL = env("DATABASE_URL")
if _DATABASE_URL:
    import dj_database_url

    _DATABASE_URL = _DATABASE_URL.replace("-pooler.", ".")
    DATABASES = {
        "default": dj_database_url.parse(
            _DATABASE_URL, conn_max_age=600, ssl_require=True
        )
    }
else:
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

# --- Auth -------------------------------------------------------------------
AUTH_USER_MODEL = "core.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- DRF --------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.DefaultPagination",
    "PAGE_SIZE": 25,
    "EXCEPTION_HANDLER": "apps.core.exceptions.crm_exception_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Real Estate CRM API",
    "DESCRIPTION": "Multi-tenant real estate CRM — Phase 1 MVP.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

# --- Celery -----------------------------------------------------------------
# Optional. On the single-service (Render free) staging deploy there is no broker/worker,
# so the SLA sweep is run via `python manage.py sweep_sla` (external cron) instead of Beat.
# When REDIS_URL is absent, tasks run eagerly (inline) so any .delay() still works.
_REDIS_URL = env("REDIS_URL")
CELERY_BROKER_URL = _REDIS_URL or "memory://"
CELERY_RESULT_BACKEND = _REDIS_URL or "cache+memory://"
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", not _REDIS_URL)
CELERY_BEAT_SCHEDULE = {
    "lead-sla-sweep": {
        "task": "apps.leads.tasks.sweep_sla_breaches",
        "schedule": 300.0,  # every 5 minutes (SRS §3.1.9) — only when Beat is deployed
    },
}

# --- Cache ------------------------------------------------------------------
# Redis when available; otherwise in-process locmem (fine for a single web service).
if _REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _REDIS_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }

# --- Object storage (S3 / MinIO) --------------------------------------------
# Only wired to S3 when AWS_STORAGE_BUCKET_NAME is set; otherwise local files.
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
if AWS_STORAGE_BUCKET_NAME:
    STORAGES = {
        "default": {"BACKEND": "storages.backends.s3.S3Storage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")
    AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL")
    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", "us-east-1")
    AWS_S3_USE_SSL = env_bool("AWS_S3_USE_SSL", False)
    AWS_QUERYSTRING_AUTH = True

# --- i18n / tz --------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True  # store UTC, render per-user tz (plan §2.6)

# --- Static / media ---------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# WhiteNoise serves admin/Swagger static from the app (no separate static host needed).
if not AWS_STORAGE_BUCKET_NAME:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        },
    }
else:
    STORAGES["staticfiles"] = {  # noqa: F821 (STORAGES defined in the S3 block above)
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- CORS -------------------------------------------------------------------
# Dev defaults + the deployed frontend origin (Vercel) via FRONTEND_ORIGIN.
CORS_ALLOWED_ORIGINS = [
    o
    for o in env(
        "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if o
]
_frontend_origin = env("FRONTEND_ORIGIN")
if _frontend_origin:
    CORS_ALLOWED_ORIGINS.append(_frontend_origin)
CORS_ALLOW_CREDENTIALS = True

# --- Integration adapters (plan §2.5) ---------------------------------------
# Swap these paths for real providers (Twilio, SendGrid) without touching core.
SMS_PROVIDER = env("SMS_PROVIDER", "apps.integrations.providers.ConsoleSMSProvider")
EMAIL_PROVIDER = env(
    "EMAIL_PROVIDER", "apps.integrations.providers.ConsoleEmailProvider"
)

# --- Lead SLA (plan §6.3 / SRS 3.1.9) ---------------------------------------
LEAD_SLA_MINUTES = int(env("LEAD_SLA_MINUTES", "60"))
