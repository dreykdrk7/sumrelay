"""Card setup through SumUp's Widget; all reusable credentials stay on the server."""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .gateway import ProviderError, ProviderRejected, SumUpGateway
from .models import Attempt, SavedCard
from .services import installation_config, refresh_attempt
from .validation import PaymentError, validate_request


def cipher():
    try:
        return Fernet(settings.PAYMENT_TOKEN_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError):
        raise PaymentError(
            "cards_not_configured", "Falta una clave válida para cifrar tokens.", 503
        ) from None


def token_for(card):
    if card.status != "ready" or card.uncertain or not card.token_ciphertext:
        raise ProviderError("La tarjeta guardada no está verificada.")
    try:
        return cipher().decrypt(card.token_ciphertext.encode()).decode()
    except (InvalidToken, UnicodeError):
        raise ProviderError("No se puede descifrar el instrumento guardado.") from None


def serialize_card(card):
    return {
        "id": str(card.id),
        "reference": card.reference,
        "merchant_code": card.merchant_code,
        "status": card.status,
        "uncertain": card.uncertain,
        "sandbox": card.sandbox,
        "setup_checkout_id": card.setup_checkout_id,
        "last4": card.last4 or None,
        "expires_at": card.expires_at.isoformat(),
        "last_error": card.last_error or None,
        "checked_at": card.checked_at.isoformat() if card.checked_at else None,
    }


def _account(card, gateway):
    account = gateway.account(card.merchant_code)
    if (
        account["merchant_code"] != card.merchant_code
        or account["currency"] != "EUR"
        or (settings.SUMUP_SANDBOX_ONLY and account["sandbox"] is not True)
    ):
        raise ProviderError("Comercio no válido para el alta de tarjeta.")
    card.sandbox = account["sandbox"] is True


def _apply(card, data, gateway):
    try:
        valid = (
            data.get("id")
            and data.get("checkout_reference") == str(card.id)
            and (not card.setup_checkout_id or data["id"] == card.setup_checkout_id)
            and data.get("merchant_code") == card.merchant_code
            and data.get("customer_id") == card.customer_id
            and data.get("currency") == "EUR"
            and Decimal(str(data.get("amount"))) == 1
        )
    except (InvalidOperation, TypeError, ValueError):
        valid = False
    if not valid:
        raise ProviderError("El alta de tarjeta no coincide con el cliente y el comercio.")
    card.setup_checkout_id = data["id"]
    status = data.get("status")
    if status == "PAID":
        transaction_id = data.get("transaction_id")
        if not transaction_id:
            raise ProviderError("Falta la transacción del alta de tarjeta.")
        detail = gateway.transaction(card.merchant_code, transaction_id)
        if (
            detail.get("id") != transaction_id
            or detail.get("status") not in {"SUCCESSFUL", "REFUNDED"}
            or detail.get("merchant_code") != card.merchant_code
            or detail.get("currency") != "EUR"
            or Decimal(str(detail.get("amount"))) != 1
        ):
            raise ProviderError("No se confirma la autorización de alta.")
        source = (data.get("payment_instrument") or {}).get("token")
        instruments = [
            item
            for item in gateway.instruments(card.customer_id)
            if item.get("active") is True and (not source or item.get("token") == source)
        ]
        if len(instruments) != 1 or not instruments[0].get("token"):
            raise ProviderError("No hay un único instrumento activo confirmado.")
        instrument = instruments[0]
        mandate = instrument.get("mandate") or {}
        if (
            mandate.get("merchant_code") != card.merchant_code
            or mandate.get("status") != "active"
            or mandate.get("type") != "recurrent"
        ):
            raise ProviderError("No hay un mandato recurrente activo para este comercio.")
        card.token_ciphertext = cipher().encrypt(instrument["token"].encode()).decode()
        card.last4 = (instrument.get("card") or {}).get("last_4_digits") or ""
        card.status = "ready"
    elif status in {"EXPIRED", "FAILED"}:
        card.status = "expired" if status == "EXPIRED" else "failed"
    elif status != "PENDING":
        raise ProviderError("Estado de alta desconocido.")
    card.uncertain = False
    card.last_error = ""
    card.checked_at = timezone.now()


def start_card(data, gateway=None):
    from .controls import require_creation

    require_creation(recurring=True)
    config = installation_config()
    cipher()  # Fail before creating provider resources if encryption is unconfigured.
    if (
        not isinstance(data, dict)
        or set(data) != {"reference", "return_url"}
        or not isinstance(data["reference"], str)
        or not 1 <= len(data["reference"]) <= 120
        or any(ord(c) < 32 for c in data["reference"])
        or data["return_url"] not in config.allowed_return_urls
    ):
        raise PaymentError("invalid_request", "Referencia o retorno de alta inválidos.")
    with transaction.atomic():
        card, _ = SavedCard.objects.get_or_create(
            reference=data["reference"],
            defaults={
                "merchant_code": settings.SUMUP_MERCHANT_CODE,
                "return_url": data["return_url"],
                "expires_at": timezone.now() + timedelta(seconds=config.checkout_ttl_seconds),
            },
        )
        card = SavedCard.objects.select_for_update().get(pk=card.pk)
        if (
            card.return_url != data["return_url"]
            or card.merchant_code != settings.SUMUP_MERCHANT_CODE
        ):
            raise PaymentError("setup_conflict", "La referencia de alta tiene otros datos.", 409)
        submit = not card.create_submitted_at
        if submit:
            card.create_submitted_at = timezone.now()
            card.save()
    if gateway is None:
        with SumUpGateway() as provider:
            return _start_reserved(card, submit, provider)
    return _start_reserved(card, submit, gateway)


def _start_reserved(card, submit, gateway):
    from .controls import require_creation

    require_creation(recurring=True)
    if not submit:
        return refresh_card(card.id, gateway)
    # The reservation was committed before POST. An unknown result is only recovered by GET.
    try:
        _account(card, gateway)
        gateway.ensure_customer(card)
        data = gateway.create_card_setup(card)
        _apply(card, data, gateway)
    except ProviderRejected:
        card.status, card.uncertain = "failed", False
        card.last_error = "SumUp ha rechazado el alta de tarjeta."
    except ProviderError:
        card.uncertain = True
        card.last_error = "Alta pendiente de verificar; no se creará otra automáticamente."
    card.save()
    return card


def refresh_card(card_id, gateway=None):
    if gateway is None:
        with SumUpGateway() as provider:
            return refresh_card(card_id, provider)
    with transaction.atomic():
        card = SavedCard.objects.select_for_update().get(pk=card_id)
        try:
            _account(card, gateway)
            if card.setup_checkout_id:
                data = gateway.get(card.setup_checkout_id)
            else:
                matches = gateway.find(str(card.id))
                if len(matches) != 1:
                    raise ProviderError("Alta sin resolver; no hay un único checkout.")
                data = matches[0]
            _apply(card, data, gateway)
        except (ProviderError, InvalidOperation, TypeError, ValueError):
            card.uncertain = True
            card.last_error = "No se pudo verificar el instrumento guardado."
        card.checked_at = timezone.now()
        card.save()
        return card


def process_recurring(attempt_id, gateway=None):
    # A paused deployment can still reconcile old charges, but cannot submit one.
    if not settings.PAYMENT_CREATION_ENABLED or not settings.RECURRING_CHARGES_ENABLED:
        return Attempt.objects.get(pk=attempt_id)
    if gateway is None:
        with SumUpGateway() as provider:
            return process_recurring(attempt_id, provider)
    from .models import Payment

    # Durable submission marker MUST be committed before PUT; never automatically resend it.
    with transaction.atomic():
        payment_id = Attempt.objects.values_list("payment_id", flat=True).get(pk=attempt_id)
        Payment.objects.select_for_update().get(pk=payment_id)
        attempt = Attempt.objects.select_related("payment__saved_card").get(pk=attempt_id)
        if (
            not attempt.payment.saved_card_id
            or attempt.payment.cancel_requested_at
            or attempt.process_submitted_at
            or attempt.uncertain
            or attempt.status != "pending"
            or not attempt.sumup_id
        ):
            return attempt
        try:
            token = token_for(attempt.payment.saved_card)
        except ProviderError:
            return attempt
        attempt.process_submitted_at = timezone.now()
        attempt.save(update_fields=["process_submitted_at"])
    try:
        data = gateway.process_card(attempt, token)
        Attempt.objects.filter(pk=attempt.id).update(action_required=bool(data.get("next_step")))
    except ProviderError:
        # PUT can have succeeded; only the checkout/transaction will decide the outcome.
        pass
    return refresh_attempt(attempt_id, gateway)


def cancel_recurring_order(data, gateway=None):
    """Durable cancellation tombstone, even if the original HTTP create arrives later."""
    from uuid import UUID

    from .models import Payment
    from .services import cancel_attempt

    if not isinstance(data, dict) or not isinstance(data.get("saved_card_id"), str):
        raise PaymentError("invalid_request", "Indica el pedido y la tarjeta guardada.")
    body = dict(data)
    try:
        card_id = UUID(body.pop("saved_card_id"))
        card = SavedCard.objects.get(pk=card_id, merchant_code=settings.SUMUP_MERCHANT_CODE)
    except (ValueError, SavedCard.DoesNotExist):
        raise PaymentError("card_not_found", "Tarjeta guardada no encontrada.", 404) from None
    normalized, amount, _ = validate_request(body, "cancel-order", installation_config())
    with transaction.atomic():
        payment, _ = Payment.objects.get_or_create(
            reference=normalized["reference"],
            defaults={
                "amount": amount,
                "currency": normalized["currency"],
                "merchant_code": card.merchant_code,
                "saved_card": card,
            },
        )
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if (
            payment.amount != amount
            or payment.currency != normalized["currency"]
            or payment.merchant_code != card.merchant_code
            or payment.saved_card_id != card.id
        ):
            raise PaymentError("order_conflict", "La referencia pertenece a otro pedido.", 409)
        payment.cancel_requested_at = payment.cancel_requested_at or timezone.now()
        payment.save(update_fields=["cancel_requested_at"])
    for attempt in payment.attempts.filter(closed_confirmed=False):
        cancel_attempt(attempt.id, None, gateway)
    return payment
