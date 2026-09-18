"""
Base Django settings for the Real Estate CRM.

Shared across dev/prod. Environment-specific overrides live in dev.py / prod.py.
See architecture.md "Architecture Principles" and §1.3 for the stack rationale.
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
    "django.contrib.gis",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    # Server-side token revocation. Without it `is_active=False` is the only way to end a
    # session, and a refresh token stays exchangeable for its full 7-day life.
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "django_filters",
    "corsheaders",
]

# Build/migration order matters: core -> identity -> contacts -> inventory ->
# crm -> property_ops -> finance -> collaboration -> platform (see
# architecture.md's import DAG / implementation roadmap).
LOCAL_APPS = [
    "apps.core",
    "apps.identity",
    "apps.contacts",
    "apps.inventory",
    "apps.crm",
    "apps.property_ops",
    "apps.finance",
    "apps.collaboration",
    "apps.platform",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# --- Middleware -------------------------------------------------------------
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
# Neon's `-pooler` endpoint is a transaction pooler, which breaks anything relying on a
# stable per-connection session (advisory locks, SET LOCAL, SELECT ... FOR UPDATE spanning
# statements — architecture.md §2's reference-code generator needs the last of these), so we
# rewrite it to the direct host. Direct connections suit a persistent gunicorn server anyway.
# ENGINE is the GeoDjango/PostGIS backend (architecture.md's geo_point columns require it).
_GIS_ENGINE = "django.contrib.gis.db.backends.postgis"
_DATABASE_URL = env("DATABASE_URL")
if _DATABASE_URL:
    import dj_database_url

    _DATABASE_URL = _DATABASE_URL.replace("-pooler.", ".")
    DATABASES = {
        "default": dj_database_url.parse(
            _DATABASE_URL, conn_max_age=600, ssl_require=True
        )
    }
    DATABASES["default"]["ENGINE"] = _GIS_ENGINE
else:
    DATABASES = {
        "default": {
            "ENGINE": _GIS_ENGINE,
            "NAME": env("POSTGRES_DB", "crm"),
            # Must match the non-superuser role deploy/postgres-init.sql creates:
            # apps/core/db_policy.py reads this value at migrate time to build the
            # append-only REVOKE statements. A mismatch silently revokes from nobody.
            "USER": env("POSTGRES_USER", "crm_app"),
            "PASSWORD": env("POSTGRES_PASSWORD", "crm"),
            "HOST": env("POSTGRES_HOST", "localhost"),
            "PORT": env("POSTGRES_PORT", "5432"),
        }
    }

# --- Auth -------------------------------------------------------------------
AUTH_USER_MODEL = "identity.User"

# auth.E003 requires USERNAME_FIELD to carry a plain unique=True. architecture.md §4 instead
# specifies a PARTIAL unique index on identity_user.email (WHERE deleted_at IS NULL), so that
# soft-deleting a user releases their address for reuse. The uniqueness is real, just
# conditional, and it is enforced by the identity_user_email_uniq constraint on the model.
# Callers that resolve a user by email must filter deleted_at__isnull=True — see the note on
# apps.identity.models.User.
SILENCED_SYSTEM_CHECKS = ["auth.E003"]

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
    # PasswordIsCurrent is a default rather than opt-in per view: a user whose initial
    # password was chosen by their registrar must be confined to changing it, and one
    # forgotten view would make that a suggestion. Views that must stay reachable meanwhile
    # (/auth/me/, change-password) set `allow_stale_password = True`.
    # StaffWrite is a default rather than opt-in for the same reason PasswordIsCurrent is:
    # the failure it prevents is not a wrong rule on one endpoint, it is an endpoint added in
    # a later pass that nobody remembers to protect. Row scoping has no opinion about
    # configuration tables, so without a function-level floor a rental tenant could write
    # one. Views that genuinely serve client writes set `portal_writable = True`.
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
        "apps.identity.api.permissions.PasswordIsCurrent",
        "apps.identity.permissions.StaffWrite",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.DefaultPagination",
    "PAGE_SIZE": 25,
    "EXCEPTION_HANDLER": "apps.core.exceptions.crm_exception_handler",
    # Login is reachable unauthenticated by definition, so it is the one endpoint that must
    # be rate-limited from the start. Other scopes are added as their endpoints land.
    "DEFAULT_THROTTLE_CLASSES": ("rest_framework.throttling.ScopedRateThrottle",),
    "DEFAULT_THROTTLE_RATES": {
        "login": env("LOGIN_THROTTLE_RATE", "10/min"),
        # Password reset is public and sends mail, so it is both a brute-force and a
        # mail-flooding vector.
        "password_reset": env("PASSWORD_RESET_THROTTLE_RATE", "5/min"),
        "file_upload": env("FILE_UPLOAD_THROTTLE_RATE", "30/min"),
        # Public token-URL signing: unauthenticated by design, so throttled by design.
        "esign_public": env("ESIGN_PUBLIC_THROTTLE_RATE", "30/min"),
        # /api/public/ — anonymous by design, throttled by design.
        "public_listings": env("PUBLIC_LISTINGS_THROTTLE_RATE", "60/min"),
        "public_inquiry": env("PUBLIC_INQUIRY_THROTTLE_RATE", "5/min"),
        "public_page": env("PUBLIC_PAGE_THROTTLE_RATE", "30/min"),
        "webhook_inbound": env("WEBHOOK_INBOUND_THROTTLE_RATE", "120/min"),
        "public_chat": env("PUBLIC_CHAT_THROTTLE_RATE", "20/min"),
    },
}

SIMPLE_JWT = {
    # 15 minutes, not 60. A JWT cannot be recalled once issued, so the access-token lifetime
    # IS the window in which a deactivated user, a revoked role, or a suspended portal client
    # still has reach. Shortening it is the only real mitigation short of introspecting every
    # request against the database, which would defeat the point of a stateless token.
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    # Rotate on refresh and blacklist the old token, so a stolen refresh token stops working
    # as soon as the legitimate holder next refreshes.
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Real Estate CRM API",
    "DESCRIPTION": "Single-company real estate CRM — foundation/data-model pass.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    # drf-spectacular names an enum component after the *field*, so every model with a
    # `status` column would claim the name "StatusEnum" and the loser gets a generated suffix
    # like "Status44fEnum" — a name that changes whenever the components are reordered, which
    # breaks every regenerated client. Each one is named explicitly instead.
    "ENUM_NAME_OVERRIDES": {
        "ContractRefTypeEnum": "apps.identity.models.PortalProfile.ContractRefType",
        "PropertyStatusEnum": "apps.inventory.models.Property.Status",
        "UnitStatusEnum": "apps.inventory.models.Unit.Status",
        "ListingStatusEnum": "apps.inventory.models.Listing.Status",
        "LeadStatusEnum": "apps.crm.models.Lead.Status",
        "DealStatusEnum": "apps.crm.models.Deal.Status",
        # Notification.type and NotificationPreference.notification_type serialize the same
        # choice set under two field names; one canonical component name for both.
        "NotificationTypeEnum": "apps.collaboration.models.Notification.Type",
        # Application.background_check_status and credit_check_status share one choice set;
        # Lead.Priority and MaintenanceRequest.Priority collide on the field name.
        "ScreeningCheckStatusEnum": "apps.property_ops.models.Application.CheckStatus",
        "LeadPriorityEnum": "apps.crm.models.Lead.Priority",
        "MaintenancePriorityEnum": "apps.property_ops.models.MaintenanceRequest.Priority",
        # Thread.channel adds CALL/MIXED to the Message/Template EMAIL/SMS/WHATSAPP set;
        # same field name, two choice sets.
        "ThreadChannelEnum": "apps.collaboration.models.Thread.Channel",
        "MessageChannelEnum": "apps.collaboration.models.Message.Channel",
        # Offer.direction (buyer/seller) vs Message/CallLog.direction (inbound/outbound).
        "OfferDirectionEnum": "apps.crm.models.Offer.Direction",
        "MessageDirectionEnum": "apps.collaboration.models.Message.Direction",
    },
}

# --- Celery -----------------------------------------------------------------
# Optional. When REDIS_URL is absent, tasks run eagerly (inline) so any .delay() still
# works. No CELERY_BEAT_SCHEDULE yet — the per-app tasks.py modules are still empty stubs.
# The services pass fills them in and adds the schedules architecture.md §1.3 lists: rent
# recurrence (finance/property_ops), SLA sweeps (crm), report snapshots and webhook delivery
# (platform), notification dispatch (collaboration), MLS/portal sync (platform).
_REDIS_URL = env("REDIS_URL")
CELERY_BROKER_URL = _REDIS_URL or "memory://"
CELERY_RESULT_BACKEND = _REDIS_URL or "cache+memory://"
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", not _REDIS_URL)

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

# --- Domain settings --------------------------------------------------------
# SRS 3.1.9: "flag and alert on leads with no follow-up activity within a configurable SLA
# window." Configurable is the operative word — it varies by market and by team.
LEAD_SLA_MINUTES = int(env("LEAD_SLA_MINUTES", "60"))

# The region a bare local phone number is assumed to belong to when normalising to E.164.
# De-duplication (SRS 3.1.3 / 3.1.10) matches on the normalised number, so "050 123 4567" and
# "+971 50 123 4567" must collapse to one key — and that rule is per-country, which is why
# this is configuration rather than a constant. An ISO 3166-1 alpha-2 code.
DEFAULT_PHONE_REGION = env("DEFAULT_PHONE_REGION", "AE")

# Trigram similarity at or above which two names are offered as a possible duplicate. Low
# enough to catch a transposition or a missing letter, high enough that a common first name
# alone does not flood the suggestion list.
CONTACT_SIMILARITY_THRESHOLD = float(env("CONTACT_SIMILARITY_THRESHOLD", "0.4"))

# A CSV import runs inline (no Celery on the free tier), so the request has to finish inside
# the gateway timeout. Rows beyond this are refused with a message rather than truncated.
CONTACT_IMPORT_MAX_ROWS = int(env("CONTACT_IMPORT_MAX_ROWS", "5000"))

# --- Files (collaboration) --------------------------------------------------
FILE_UPLOAD_MAX_BYTES = int(env("FILE_UPLOAD_MAX_BYTES", str(25 * 1024 * 1024)))

# --- Channel gateways (collaboration) ---------------------------------------
# EMAIL is real (Django mail). SMS/WhatsApp/push are logging mocks until real providers are
# integrated — each mock's counterpart is documented in third-part-needed.md. Swapping is a
# settings change: point the channel at any class implementing gateways.MessageGateway.
# Where a signer's token URL points. Relative by default — the frontend composes its own
# origin; set an absolute template (https://app.example.com/sign/{token}) in production.
ESIGN_PUBLIC_URL_TEMPLATE = env("ESIGN_PUBLIC_URL_TEMPLATE", "/public/esign/{token}/")
ESIGN_TOKEN_MAX_AGE_DAYS = int(env("ESIGN_TOKEN_MAX_AGE_DAYS", "30"))

# Outbound webhook delivery: "mock" (importable outbox, mail.outbox ergonomics) or
# "urllib" (stdlib POST, 5s timeout). `requests` with retries is the documented
# production hardening (third-part-needed.md).
# AI assistance (SRS 3.20): "mock" = the deterministic rule-based provider in
# apps/crm/ai.py. A Claude API provider is documented in third-part-needed.md §17;
# point AI_PROVIDER_CLASS at it once the anthropic SDK and key are in place.
AI_PROVIDER = env("AI_PROVIDER", "mock")

WEBHOOK_TRANSPORT = env("WEBHOOK_TRANSPORT", "urllib")

COLLABORATION_GATEWAYS = {
    "EMAIL": env("GATEWAY_EMAIL", "apps.collaboration.gateways.DjangoEmailGateway"),
    "SMS": env("GATEWAY_SMS", "apps.collaboration.gateways.LoggingSmsGateway"),
    "WHATSAPP": env("GATEWAY_WHATSAPP", "apps.collaboration.gateways.LoggingWhatsAppGateway"),
    "PUSH": env("GATEWAY_PUSH", "apps.collaboration.gateways.LoggingPushGateway"),
}

# --- Email ------------------------------------------------------------------
# Used by the password-reset flow. Console backend by default: nothing is configured for real
# delivery yet, and silently dropping a reset link is worse than printing it. Production sets
# EMAIL_BACKEND plus the EMAIL_HOST_* vars.
EMAIL_BACKEND = env(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("EMAIL_PORT", "25"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", False)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "no-reply@example.com")

# --- i18n / tz --------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True  # TIMESTAMPTZ in UTC everywhere (architecture.md §2)

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
FRONTEND_ORIGIN = _frontend_origin or "http://localhost:5173"

# Integration adapters (pluggable SMS/email/e-sign/payment providers) and lead-SLA config
# belong to the platform/crm services passes — not built in this models-only foundation
# pass. Re-add here once apps/platform/providers.py (or similar) exists.
