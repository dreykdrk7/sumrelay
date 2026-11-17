import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .gateway import ProviderError, ProviderRejected, SumUpGateway, safe_checkout_url
from .models import (
    Attempt,
    AuditLog,
    IntegrationConfig,
    Notification,
    Payment,
    RefundEvent,
)
from .serializers import serialize_payment
from .validation import PaymentError, validate_request


def installation_config():
    config = IntegrationConfig.objects.filter(pk=1).first()
    if config is None:
        raise PaymentError(
            "installation_not_configured", "Configura la instalación en el panel.", 503
        )
    return config


def audit(actor, action, target, outcome):
    AuditLog.objects.create(
        actor=actor,
        actor_name=actor.get_username() if actor else "API del SaaS",
        action=action,
        target=str(target),
        outcome=outcome[:250],
    )


def state_signature(attempt):
    return (
        attempt.status,
        attempt.uncertain,
        attempt.closed_confirmed,
        attempt.sumup_id,
        attempt.checkout_url,
        attempt.transaction_id,
        attempt.refunded_amount,
        tuple(
            attempt.refund_events.order_by("provider_id").values_list(
                "provider_id", "amount", "status"
            )
        ),
    )


def enqueue_change(attempt):
    config = installation_config()
    now = timezone.now()
    event_id = uuid.uuid4()
    Notification.objects.create(
        id=event_id,
        attempt=attempt,
        target_url=config.notification_url,
        deadline_at=now + timedelta(seconds=settings.NOTIFICATION_RETRY_SECONDS),
        next_attempt_at=now,
        payload={
            "id": str(event_id),
            "type": "payment.updated",
            "created_at": now.isoformat(),
            "payment": serialize_payment(attempt.payment),
        },
    )


def _mark_error(attempt, message):
    attempt.uncertain = True
    attempt.last_error = message
    attempt.last_checked_at = timezone.now()


def _save_attempt(attempt):
    # A concurrent callback can update its queue flag without taking the order lock.
    attempt.save(
        update_fields=[
            field.name
            for field in Attempt._meta.concrete_fields
            if field.name not in {"id", "check_requested_at"}
        ]
    )


def _validate_identity(data, attempt, *, checkout=True):
    payment = attempt.payment
    try:
        valid = (
            Decimal(str(data.get("amount"))) == payment.amount
            and data.get("currency") == payment.currency
            and data.get("merchant_code") == payment.merchant_code
        )
    except InvalidOperation:
        valid = False
    if checkout:
        valid = valid and data.get("checkout_reference") == str(attempt.id) and bool(data.get("id"))
        if payment.saved_card_id:
            valid = valid and data.get("customer_id") == payment.saved_card.customer_id
        if attempt.sumup_id:
            valid = valid and data.get("id") == attempt.sumup_id
    if not valid:
        raise ProviderError("Discrepancia de identidad, importe, moneda o comercio en SumUp.")


def _refunds(data, attempt):
    events = {}
    for event in (data.get("events") or []) + (data.get("transaction_events") or []):
        if (event.get("type") or event.get("event_type")) != "REFUND":
            continue
        try:
            amount = Decimal(str(event.get("amount")))
            if not amount.is_finite() or amount <= 0 or amount > attempt.payment.amount:
                raise InvalidOperation
            if amount != amount.quantize(Decimal("0.01")) or event.get("id") is None:
                raise InvalidOperation
        except InvalidOperation:
            raise ProviderError("Evento de devolución incompleto o inválido.") from None
        event_id = str(event["id"])
        record = {
            "amount": amount,
            "status": event.get("status") or "UNKNOWN",
            "occurred_at": parse_datetime(event.get("timestamp") or ""),
        }
        if event_id in events and events[event_id] != record:
            raise ProviderError("Datos contradictorios en un evento de devolución.")
        events[event_id] = record
    combined = {
        e.provider_id: {"amount": e.amount, "status": e.status} for e in attempt.refund_events.all()
    }
    combined.update(events)
    total = sum(
        (e["amount"] for e in combined.values() if e["status"] in {"REFUNDED", "SUCCESSFUL"}),
        Decimal("0"),
    )
    if total > attempt.payment.amount or total < attempt.refunded_amount:
        raise ProviderError("Total de devoluciones inconsistente con el registro anterior.")
    if data.get("status") == "REFUNDED" and total == 0:
        raise ProviderError("Devolución indicada sin eventos confirmados suficientes.")
    return events, total


def _apply_checkout(attempt, data, gateway, *, cancelling=False):
    _validate_identity(data, attempt)
    attempt.sumup_id = data["id"]
    attempt.checkout_url = (
        safe_checkout_url(data.get("hosted_checkout_url")) or attempt.checkout_url
    )
    status = data.get("status")
    if status == "PAID":
        candidates = [
            t
            for t in data.get("transactions") or []
            if t.get("status") in {"SUCCESSFUL", "REFUNDED"} and t.get("id")
        ]
        if len(candidates) > 1:
            raise ProviderError("Hay varias transacciones cobradas para el mismo checkout.")
        tx_id = data.get("transaction_id") or (candidates[0]["id"] if candidates else None)
        if not tx_id:
            raise ProviderError("Checkout pagado sin transacción verificable.")
        detail = gateway.transaction(attempt.payment.merchant_code, tx_id)
        _validate_identity(detail, attempt, checkout=False)
        if detail.get("id") != tx_id or detail.get("status") not in {"SUCCESSFUL", "REFUNDED"}:
            raise ProviderError("Resultado de la transacción todavía no verificable.")
        if attempt.payment.saved_card_id and detail.get("payment_type") != "RECURRING":
            raise ProviderError("El cargo no confirma el instrumento recurrente esperado.")
        if attempt.transaction_id and attempt.transaction_id != tx_id:
            raise ProviderError("La transacción confirmada ha cambiado.")
        events, total = _refunds(detail, attempt)
        for event_id, record in events.items():
            RefundEvent.objects.update_or_create(
                attempt=attempt,
                provider_id=event_id,
                defaults=record,
            )
        attempt.transaction_id = tx_id
        attempt.transaction_code = detail.get("transaction_code") or ""
        attempt.paid_at = attempt.paid_at or timezone.now()
        attempt.refunded_amount = total
        attempt.status = (
            Attempt.Status.REFUNDED
            if total == attempt.payment.amount
            else Attempt.Status.PARTIALLY_REFUNDED
            if total
            else Attempt.Status.PAID
        )
        attempt.closed_confirmed = True
    elif attempt.paid_at:
        raise ProviderError("SumUp devuelve un estado incompatible con un cobro ya confirmado.")
    elif status == "EXPIRED":
        attempt.status = (
            Attempt.Status.CANCELLED
            if cancelling or attempt.status == Attempt.Status.CANCELLED
            else Attempt.Status.EXPIRED
        )
        attempt.closed_confirmed = True
    elif status in {"PENDING", "FAILED"}:
        if attempt.closed_confirmed:
            raise ProviderError("SumUp vuelve a mostrar cobrable un checkout cerrado.")
        attempt.status = Attempt.Status.PENDING if status == "PENDING" else Attempt.Status.FAILED
        # FAILED may describe just the latest processing attempt: it does not authorize a retry.
        attempt.closed_confirmed = False
        if cancelling:
            raise ProviderError("La desactivación aún no está confirmada por SumUp.")
    else:
        raise ProviderError("Estado de checkout desconocido; requiere comprobación.")
    attempt.uncertain = False
    attempt.last_error = ""
    attempt.last_checked_at = timezone.now()


def refresh_attempt(attempt_id, gateway=None, *, cancelling=False):
    if gateway is None:
        with SumUpGateway() as provider:
            return refresh_attempt(attempt_id, provider, cancelling=cancelling)
    payment_id = Attempt.objects.values_list("payment_id", flat=True).get(pk=attempt_id)
    with transaction.atomic():
        # All state transitions for an order share this lock, including API and admin actions.
        Payment.objects.select_for_update().get(pk=payment_id)
        attempt = Attempt.objects.select_related("payment").get(pk=attempt_id)
        before = state_signature(attempt)
        requested_at = attempt.check_requested_at
        try:
            if attempt.sumup_id:
                data = gateway.get(attempt.sumup_id)
            elif attempt.closed_confirmed and attempt.status in {
                Attempt.Status.FAILED,
                Attempt.Status.CANCELLED,
            }:
                # A rejected creation has no provider resource to look up.
                data = None
            else:
                # Recover a lost create response by our stable reference; never POST again.
                matches = gateway.find(str(attempt.id))
                if len(matches) != 1:
                    raise ProviderError("Creación sin resolver: no se encontró un único checkout.")
                data = matches[0]
            if data is not None:
                _apply_checkout(attempt, data, gateway, cancelling=cancelling)
                if attempt.payment.saved_card_id and attempt.status == Attempt.Status.FAILED:
                    gateway.deactivate(attempt.sumup_id)
                    _apply_checkout(
                        attempt, gateway.get(attempt.sumup_id), gateway, cancelling=True
                    )
        except ProviderError as exc:
            _mark_error(attempt, str(exc))
        _save_attempt(attempt)
        # Do not lose a webhook that arrived while the provider request was in flight.
        Attempt.objects.filter(pk=attempt.pk, check_requested_at=requested_at).update(
            check_requested_at=None
        )
        if before != state_signature(attempt):
            enqueue_change(attempt)
        return attempt


def create_payment(data, key, gateway=None, *, saved_card_id=None):
    from .controls import require_creation

    require_creation(recurring=saved_card_id is not None)
    config = installation_config()
    normalized, amount, digest = validate_request(data, key, config)
    card = None
    if saved_card_id is not None:
        import hashlib

        from .cards import token_for
        from .models import SavedCard

        try:
            card = SavedCard.objects.get(
                pk=saved_card_id, merchant_code=settings.SUMUP_MERCHANT_CODE
            )
        except (SavedCard.DoesNotExist, ValueError, ValidationError):
            raise PaymentError("card_not_found", "Tarjeta guardada no encontrada.", 404) from None
        try:
            token_for(card)
        except ProviderError:
            raise PaymentError(
                "card_not_ready", "La tarjeta todavía no está confirmada.", 409
            ) from None
        digest = hashlib.sha256(f"{digest}:{card.id}".encode()).hexdigest()
    if not settings.SUMUP_API_KEY or not settings.SUMUP_MERCHANT_CODE:
        raise PaymentError("provider_not_configured", "Faltan las credenciales de SumUp.", 503)
    existing = Attempt.objects.filter(idempotency_key=key).first()
    if existing:
        if existing.request_hash != digest:
            raise PaymentError("idempotency_conflict", "La clave ya se usó con otros datos.", 409)
        return existing, False
    previous = Payment.objects.filter(reference=normalized["reference"]).first()
    if previous:
        # Resolve prior attempts before deciding whether another one can be created.
        for attempt in previous.attempts.all():
            refresh_attempt(attempt.id, gateway)
    try:
        with transaction.atomic():
            payment, _ = Payment.objects.get_or_create(
                reference=normalized["reference"],
                defaults={
                    "amount": amount,
                    "currency": normalized["currency"],
                    "merchant_code": settings.SUMUP_MERCHANT_CODE,
                    "saved_card": card,
                },
            )
            payment = Payment.objects.select_for_update().get(pk=payment.pk)
            existing = Attempt.objects.filter(idempotency_key=key).first()
            if existing:
                if existing.request_hash != digest:
                    raise PaymentError(
                        "idempotency_conflict", "La clave ya tiene otros datos.", 409
                    )
                return existing, False
            if (
                payment.amount != amount
                or payment.currency != normalized["currency"]
                or payment.merchant_code != settings.SUMUP_MERCHANT_CODE
                or payment.saved_card_id != (card.id if card else None)
            ):
                raise PaymentError(
                    "order_conflict", "La referencia ya tiene otra instantánea.", 409
                )
            for old in payment.attempts.all():
                if old.paid_at or old.uncertain or not old.closed_confirmed:
                    raise PaymentError(
                        "attempt_blocked", "Existe un intento cobrado o sin cerrar.", 409
                    )
            if payment.cancel_requested_at:
                raise PaymentError("payment_cancelled", "Este pedido está cancelado.", 409)
            attempt = Attempt.objects.create(
                payment=payment,
                idempotency_key=key,
                request_hash=digest,
                description=normalized["description"],
                return_url=normalized["return_url"],
                expires_at=timezone.now() + timedelta(seconds=config.checkout_ttl_seconds),
                last_error="Creación pendiente de confirmar con SumUp.",
            )
    except IntegrityError:
        # A concurrent order with another reference may claim the same global key.
        existing = Attempt.objects.filter(idempotency_key=key).first()
        if existing and existing.request_hash == digest:
            return existing, False
        raise PaymentError(
            "idempotency_conflict", "La clave ya se usó en otra solicitud.", 409
        ) from None
    # Reservation is durable before the network side effect. A crash leaves a recoverable attempt.
    if gateway is None:
        with SumUpGateway() as provider:
            _create_checkout(attempt, provider)
    else:
        _create_checkout(attempt, gateway)
    attempt.refresh_from_db()
    return attempt, True


def _create_checkout(attempt, gateway):
    from .controls import require_creation

    require_creation(recurring=attempt.payment.saved_card_id is not None)
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=attempt.payment_id)
        attempt.refresh_from_db()
        if payment.cancel_requested_at:
            attempt.status = Attempt.Status.CANCELLED
            attempt.closed_confirmed = True
            attempt.uncertain = False
            attempt.last_error = "Creación cancelada antes del envío a SumUp."
            _save_attempt(attempt)
            enqueue_change(attempt)
            return
        try:
            data = gateway.create(attempt)
        except ProviderRejected as exc:
            attempt.status = Attempt.Status.FAILED
            attempt.closed_confirmed = True
            attempt.uncertain = False
            attempt.last_error = str(exc)
            attempt.last_checked_at = timezone.now()
        except ProviderError as exc:
            _mark_error(attempt, str(exc))
        else:
            try:
                _apply_checkout(attempt, data, gateway)
            except ProviderError as exc:
                # A rejection during transaction lookup cannot undo a successful creation.
                _mark_error(attempt, str(exc))
        _save_attempt(attempt)
        enqueue_change(attempt)


def cancel_attempt(attempt_id, actor, gateway=None):
    audit(actor, "cancel_requested", attempt_id, "Solicitud registrada")
    if gateway is None:
        with SumUpGateway() as provider:
            return cancel_attempt_with_gateway(attempt_id, actor, provider)
    return cancel_attempt_with_gateway(attempt_id, actor, gateway)


def cancel_attempt_with_gateway(attempt_id, actor, gateway):
    payment_id = Attempt.objects.values_list("payment_id", flat=True).get(pk=attempt_id)
    with transaction.atomic():
        Payment.objects.select_for_update().get(pk=payment_id)
        attempt = refresh_attempt(attempt_id, gateway)
        if attempt.paid_at or attempt.closed_confirmed:
            audit(actor, "cancel_result", attempt_id, f"Sin cancelación: {attempt.status}")
            return attempt
        if not attempt.sumup_id:
            audit(actor, "cancel_result", attempt_id, "No se conoce el checkout; sigue bloqueado")
            return attempt
        before = state_signature(attempt)
        try:
            response = gateway.deactivate(attempt.sumup_id)
            _validate_identity(response, attempt)
            # DELETE success alone is not proof. Re-read to handle a concurrent payment.
            confirmed = gateway.get(attempt.sumup_id)
            _apply_checkout(attempt, confirmed, gateway, cancelling=True)
        except ProviderError:
            # A request may have succeeded despite a lost response; retain conservative state.
            _mark_error(attempt, "Resultado de cancelación incierto; requiere comprobación.")
        _save_attempt(attempt)
        if before != state_signature(attempt):
            enqueue_change(attempt)
        audit(
            actor,
            "cancel_result",
            attempt_id,
            f"{attempt.status}; incierto={attempt.uncertain}; {attempt.last_error}",
        )
        return attempt
