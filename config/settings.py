import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def secret(name, default=""):
    filename = os.getenv(f"{name}_FILE")
    return Path(filename).read_text().strip() if filename else os.getenv(name, default)


DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() == "true"
SECRET_KEY = secret("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("Configura DJANGO_SECRET_KEY o DJANGO_SECRET_KEY_FILE.")
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = list(filter(None, os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")))
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "payments.apps.PaymentsConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "sumup"),
        "USER": os.getenv("POSTGRES_USER", "sumup"),
        "PASSWORD": secret("POSTGRES_PASSWORD"),
        "HOST": os.getenv("POSTGRES_HOST", "db"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"django.contrib.auth.password_validation.{name}"}
    for name in [
        "UserAttributeSimilarityValidator",
        "MinimumLengthValidator",
        "CommonPasswordValidator",
        "NumericPasswordValidator",
    ]
]
LANGUAGE_CODE = "es"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SECURE_SSL_REDIRECT = not DEBUG
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if os.getenv("TRUST_PROXY", "false").lower() == "true":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
DATA_UPLOAD_MAX_MEMORY_SIZE = 16 * 1024
SESSION_COOKIE_AGE = 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SUMUP_API_KEY = secret("SUMUP_API_KEY")
SUMUP_MERCHANT_CODE = os.getenv("SUMUP_MERCHANT_CODE", "")
SUMUP_SANDBOX_ONLY = os.getenv("SUMUP_SANDBOX_ONLY", "false").lower() == "true"
SUMUP_WEBHOOK_ENABLED = os.getenv("SUMUP_WEBHOOK_ENABLED", "true").lower() == "true"
PAYMENT_CREATION_ENABLED = os.getenv("PAYMENT_CREATION_ENABLED", "false").lower() == "true"
RECURRING_CHARGES_ENABLED = os.getenv("RECURRING_CHARGES_ENABLED", "false").lower() == "true"
PAYMENT_TOKEN_ENCRYPTION_KEY = secret("PAYMENT_TOKEN_ENCRYPTION_KEY")
SAAS_API_KEYS = list(filter(None, secret("SAAS_API_KEYS").split(",")))
SAAS_WEBHOOK_SECRET = secret("SAAS_WEBHOOK_SECRET")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
SUMUP_TIMEOUT = 15
WEBHOOK_TIMEOUT = 10
NOTIFICATION_RETRY_SECONDS = 24 * 60 * 60
PENDING_POLL_SECONDS = 60
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {"httpx": {"level": "WARNING"}, "httpcore": {"level": "WARNING"}},
}
