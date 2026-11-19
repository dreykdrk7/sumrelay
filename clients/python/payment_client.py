"""Server-side HTTP client for the processor. Depends only on httpx and Python 3.10+."""

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import UUID

import httpx


class ProcessorError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        self.code, self.status = code, status
        super().__init__(message)


class InvalidWebhook(ValueError):
    pass


class PaymentMismatch(ValueError):
    pass


class PaymentNotReady(ValueError):
    pass


@dataclass(frozen=True)
class ConfirmedPayment:
    payment_id: str
    attempt_id: str
    transaction_id: str
    reference: str
    amount: Decimal
    currency: str
    merchant_code: str


def _amount_for_request(amount, currency):
    if (
        not isinstance(amount, Decimal)
        or not amount.is_finite()
        or amount <= 0
        or amount > Decimal("999999999.99")
        or amount.as_tuple().exponent < -2
    ):
        raise ValueError("Usa un Decimal positivo, dentro de rango y con hasta dos decimales.")
    if currency == "CLP" and amount != amount.to_integral_value():
        raise ValueError("CLP exige un importe entero.")
    return format(amount, ".0f" if currency == "CLP" else "f")


class ProcessorClient:
    """No automatic POST retries: persist and reuse the same idempotency key after a timeout."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 15,
        transport: httpx.BaseTransport | None = None,
    ):
        parsed = urlsplit(base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and local))
        ):
            raise ValueError("Configura el origen HTTPS del procesador; HTTP solo para localhost.")
        if not isinstance(api_key, str) or len(api_key) < 32 or "\n" in api_key or "\r" in api_key:
            raise ValueError("Configura una credencial de servidor válida.")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise ProcessorError(
                "connection_uncertain",
                "No se recibió un resultado verificable; conserva la clave de idempotencia.",
            ) from None
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code not in {200, 201, 202}:
            error = data.get("error") if isinstance(data, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
                code = "invalid_response"
            raise ProcessorError(
                code, f"El procesador respondió HTTP {response.status_code}.", response.status_code
            )
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise ProcessorError(
                "invalid_response", "Respuesta de pago inválida.", response.status_code
            )
        return data

    def create_payment(
        self,
        *,
        reference: str,
        amount: Decimal,
        currency: str,
        description: str,
        return_url: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
            raise ValueError("Clave de idempotencia inválida.")
        return self._request(
            "POST",
            "api/v1/payments/",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "reference": reference,
                "amount": _amount_for_request(amount, currency),
                "currency": currency,
                "description": description,
                "return_url": return_url,
            },
        )

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        payment_id = str(UUID(str(payment_id)))
        return self._request("GET", f"api/v1/payments/{payment_id}/")

    def get_payment_by_reference(self, reference: str) -> dict[str, Any]:
        return self._request("GET", "api/v1/payments/", params={"reference": reference})

    def create_card_setup(self, *, reference: str, return_url: str) -> dict[str, Any]:
        return self._request(
            "POST", "api/v1/card-setups/", json={"reference": reference, "return_url": return_url}
        )

    def get_card_setup(self, card_id: str) -> dict[str, Any]:
        return self._request("GET", f"api/v1/card-setups/{UUID(str(card_id))}/")

    def create_subscription(
        self,
        *,
        reference: str,
        amount: Decimal,
        currency: str,
        description: str,
        return_url: str,
        saved_card_id: str,
        starts_on: date,
        interval_months: int,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "api/v1/subscriptions/",
            json={
                "reference": reference,
                "amount": _amount_for_request(amount, currency),
                "currency": currency,
                "description": description,
                "return_url": return_url,
                "saved_card_id": str(UUID(str(saved_card_id))),
                "starts_on": starts_on.isoformat(),
                "interval_months": interval_months,
            },
        )

    def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self._request("GET", f"api/v1/subscriptions/{UUID(str(subscription_id))}/")

    def get_subscription_by_reference(self, reference: str) -> dict[str, Any]:
        return self._request("GET", "api/v1/subscriptions/", params={"reference": reference})

    def get_subscription_payments(
        self, subscription_id: str, *, before: date | None = None
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"api/v1/subscriptions/{UUID(str(subscription_id))}/payments/",
            params={"before": before.isoformat()} if before else None,
        )

    def cancel_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self._request("POST", f"api/v1/subscriptions/{UUID(str(subscription_id))}/cancel/")


def verify_webhook(
    raw_body: bytes,
    headers: Mapping[str, str],
    secrets: str | list[str],
    *,
    now: float | None = None,
    tolerance: int = 300,
) -> dict[str, Any]:
    """Authenticate raw bytes; the SaaS must persist and deduplicate the event."""
    values = {k.lower(): v for k, v in headers.items()}
    timestamp = values.get("x-webhook-timestamp", "")
    signature = values.get("x-webhook-signature", "")
    event_id = values.get("x-webhook-id", "")
    keys = [secrets] if isinstance(secrets, str) else secrets
    if (
        len(raw_body) > 1024 * 1024
        or tolerance < 0
        or not re.fullmatch(r"[0-9]{1,12}", timestamp)
        or not re.fullmatch(r"v1=[0-9a-f]{64}", signature)
        or abs((time.time() if now is None else now) - int(timestamp)) > tolerance
    ):
        raise InvalidWebhook("Firma, fecha o tamaño de notificación inválidos.")
    valid = False
    for secret in keys:
        if len(secret) < 32:
            continue
        expected = (
            "v1="
            + hmac.new(
                secret.encode(),
                timestamp.encode() + b"." + raw_body,
                hashlib.sha256,
            ).hexdigest()
        )
        valid |= hmac.compare_digest(signature, expected)
    if not valid:
        raise InvalidWebhook("Firma de notificación inválida.")
    try:
        event = json.loads(raw_body)
        UUID(event_id)
    except (ValueError, TypeError):
        raise InvalidWebhook("Contenido de notificación inválido.") from None
    if (
        not isinstance(event, dict)
        or event.get("id") != event_id
        or not isinstance(event.get("type"), str)
    ):
        raise InvalidWebhook("Identidad de notificación inválida.")
    if event["type"] == "payment.updated" and not isinstance(event.get("payment"), dict):
        raise InvalidWebhook("Falta la instantánea del pago.")
    return event


def _money(value: Any) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,9}\.[0-9]{2}", value):
        raise PaymentMismatch("El procesador devolvió un importe inválido.")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise PaymentMismatch("El procesador devolvió un importe inválido.") from None


def checkout_url_for_redirect(payment: dict[str, Any]) -> str | None:
    """Expose an open, current checkout to Next.js after the backend has created the payment."""
    if (
        payment.get("status") != "pending"
        or payment.get("uncertain") is not False
        or payment.get("cancel_requested") is True
    ):
        return None
    attempts = payment.get("attempts")
    if not isinstance(attempts, list) or not all(isinstance(a, dict) for a in attempts):
        return None
    if any(a.get("paid_at") for a in attempts):
        return None
    selected = payment.get("requested_attempt")
    if selected is None:
        selected = next(
            (a for a in attempts if a.get("id") == payment.get("latest_attempt_id")), {}
        )
    if (
        not isinstance(selected, dict)
        or selected.get("id") != payment.get("latest_attempt_id")
        or selected.get("status") != "pending"
        or selected.get("uncertain") is not False
        or selected.get("closed_confirmed") is not False
    ):
        return None
    if any(
        a.get("closed_confirmed") is not True for a in attempts if a.get("id") != selected.get("id")
    ):
        return None
    value = selected.get("checkout_url")
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname == "checkout.sumup.com"
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
        ):
            return value
    except ValueError:
        pass
    return None


def confirm_for_order(
    payment: dict[str, Any],
    *,
    payment_id: str,
    reference: str,
    amount: Decimal,
    currency: str,
    merchant_code: str,
) -> ConfirmedPayment:
    """Validate against the SaaS order snapshot, never values supplied by the browser."""
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
        raise ValueError("El pedido debe tener un importe Decimal positivo.")
    if (
        payment.get("id") != payment_id
        or payment.get("reference") != reference
        or _money(payment.get("amount")) != amount
        or payment.get("currency") != currency
        or payment.get("merchant_code") != merchant_code
    ):
        raise PaymentMismatch("El pago no coincide con la instantánea del pedido.")
    if (
        payment.get("status") != "paid"
        or payment.get("uncertain") is not False
        or _money(payment.get("refunded_amount")) != 0
    ):
        raise PaymentNotReady("El pago todavía no autoriza la entrega del pedido.")
    attempts = payment.get("attempts")
    if not isinstance(attempts, list) or not all(isinstance(a, dict) for a in attempts):
        raise PaymentMismatch("Lista de intentos inválida.")
    paid = [a for a in attempts if a.get("paid_at")]
    if len(paid) != 1 or any(
        a.get("uncertain") is not False or a.get("closed_confirmed") is not True for a in attempts
    ):
        raise PaymentNotReady("Hay intentos pendientes, inciertos o varios cobros confirmados.")
    attempt = paid[0]
    if (
        attempt.get("status") != "paid"
        or _money(attempt.get("amount")) != amount
        or attempt.get("currency") != currency
        or attempt.get("merchant_code") != merchant_code
        or _money(attempt.get("refunded_amount")) != 0
        or not isinstance(attempt.get("transaction_id"), str)
        or not attempt["transaction_id"]
        or not isinstance(attempt.get("id"), str)
        or not attempt["id"]
    ):
        raise PaymentMismatch("El intento confirmado no coincide con el pedido.")
    return ConfirmedPayment(
        payment_id,
        attempt["id"],
        attempt["transaction_id"],
        reference,
        amount,
        currency,
        merchant_code,
    )
