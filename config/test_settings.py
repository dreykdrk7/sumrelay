import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-never-use-in-production")
os.environ.setdefault("DJANGO_DEBUG", "true")
from .settings import *  # noqa: E402,F403

if not os.getenv("TEST_POSTGRES"):
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
SAAS_API_KEYS = ["test-server-token-that-is-at-least-32-characters"]
SAAS_WEBHOOK_SECRET = "test-webhook-secret-that-is-at-least-32-characters"
SUMUP_API_KEY = "test-sumup-key"
SUMUP_MERCHANT_CODE = "MTEST123"
PAYMENT_TOKEN_ENCRYPTION_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
PUBLIC_BASE_URL = "https://payments.example.com"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

PAYMENT_CREATION_ENABLED = True
RECURRING_CHARGES_ENABLED = True
