import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from .models import SUPPORTED_CURRENCIES


class PaymentError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def validate_url(url):
    URLValidator(schemes=["https"] if not settings.DEBUG else ["http", "https"])(url)
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.fragment:
        raise ValidationError("La URL no puede contener credenciales ni fragmentos.")


def validate_request(data, key, config):
    fields = {"reference", "amount", "currency", "description", "return_url"}
    if not isinstance(data, dict) or set(data) != fields:
        raise PaymentError("invalid_request", "Envía exactamente los cinco campos documentados.")
    if not key or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        raise PaymentError("invalid_idempotency_key", "Cabecera Idempotency-Key inválida.")
    for name, limit in [("reference", 120), ("description", 500), ("return_url", 2048)]:
        value = data.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise PaymentError("invalid_request", f"Campo {name} inválido.")
        if any(ord(c) < 32 for c in value):
            raise PaymentError("invalid_request", f"Campo {name} contiene caracteres de control.")
    currency = data["currency"]
    if not isinstance(currency, str) or currency not in config.currencies:
        raise PaymentError("invalid_currency", "Moneda no habilitada en esta instalación.")
    raw = data["amount"]
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
        raise PaymentError("invalid_amount", "El importe debe ser una cadena decimal positiva.")
    try:
        amount = Decimal(raw)
        places = SUPPORTED_CURRENCIES[currency]
        if not 0 < amount <= Decimal("999999999.99") or -amount.as_tuple().exponent > places:
            raise InvalidOperation
    except (InvalidOperation, KeyError):
        raise PaymentError(
            "invalid_amount", "Importe fuera de rango o precisión inválida."
        ) from None
    if data["return_url"] not in config.allowed_return_urls:
        raise PaymentError("return_url_not_allowed", "Dirección de retorno no permitida.")
    normalized = {**data, "amount": format(amount, ".2f")}
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    return normalized, amount, digest
