"""Production / staging settings (Render single web service)."""
import os

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# Render provides RENDER_EXTERNAL_HOSTNAME; also accept an explicit list.
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "").split(",") if h]
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _render_host:
    ALLOWED_HOSTS.append(_render_host)
if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = [".onrender.com"]

# CSRF trusted origins for the deployed hosts (admin/browsable API).
CSRF_TRUSTED_ORIGINS = [f"https://{h.lstrip('.')}" for h in ALLOWED_HOSTS if h]
_frontend = env("FRONTEND_ORIGIN")
if _frontend:
    CSRF_TRUSTED_ORIGINS.append(_frontend)

# Security hardening (SRS §5.3). Behind Render's TLS-terminating proxy.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT", "true").lower() == "true"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
