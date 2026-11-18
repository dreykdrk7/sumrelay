from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def integration_checks(app_configs, **kwargs):
    errors = []
    for name in ["SUMUP_API_KEY", "SUMUP_MERCHANT_CODE", "SAAS_WEBHOOK_SECRET"]:
        if not getattr(settings, name):
            errors.append(Error(f"Falta configurar {name}.", id="payments.E001"))
    if not settings.SAAS_API_KEYS or any(len(key) < 32 for key in settings.SAAS_API_KEYS):
        errors.append(
            Error("SAAS_API_KEYS necesita claves de al menos 32 caracteres.", id="payments.E002")
        )
    if len(settings.SAAS_WEBHOOK_SECRET) < 32:
        errors.append(
            Error("SAAS_WEBHOOK_SECRET necesita al menos 32 caracteres.", id="payments.E003")
        )
    if not settings.DEBUG and urlsplit(settings.PUBLIC_BASE_URL).scheme != "https":
        errors.append(Error("PUBLIC_BASE_URL debe usar HTTPS.", id="payments.E004"))
    if settings.RECURRING_CHARGES_ENABLED:
        try:
            Fernet(settings.PAYMENT_TOKEN_ENCRYPTION_KEY.encode())
        except (ValueError, TypeError):
            errors.append(
                Error(
                    "Los cobros periódicos requieren una clave Fernet válida.", id="payments.E005"
                )
            )
    return errors
