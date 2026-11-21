from calendar import monthrange
from datetime import date, timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .cards import token_for
from .controls import require_creation
from .gateway import ProviderError
from .models import Attempt, Payment, SavedCard, Subscription
from .serializers import iso
from .services import (
    SUBSCRIPTION_REFERENCE_PREFIX,
    audit,
    installation_config,
    request_payment_cancellation,
    reserve_attempt,
)
from .validation import PaymentError, validate_request

PAYMENT_FIELDS = ("reference", "amount", "currency", "description", "return_url")


def create_subscription(data):
    fields_required = {*PAYMENT_FIELDS, "saved_card_id", "starts_on"}
    if not isinstance(data, dict) or set(data) not in (
        fields_required | {"interval_days"},
        fields_required | {"interval_months"},
    ):
        raise PaymentError(
            "invalid_request", "Indica un solo intervalo en el calendario de la suscripción."
        )
    days, months = data.get("interval_days"), data.get("interval_months")
    if "interval_days" in data:
        if type(days) is not int or days != 30:
            raise PaymentError(
                "invalid_interval", "El intervalo diario admitido es de 30 días naturales."
            )
    elif type(months) is not int or not 1 <= months <= 12:
        raise PaymentError("invalid_interval", "El intervalo debe ser de 1 a 12 meses.")
    try:
        starts = date.fromisoformat(data["starts_on"])
        if starts.isoformat() != data["starts_on"] or starts.year > 9998:
            raise ValueError
        card_id = UUID(data["saved_card_id"]) if isinstance(data["saved_card_id"], str) else None
        if card_id is None:
            raise ValueError
    except (TypeError, ValueError):
        raise PaymentError("invalid_schedule", "Fecha inicial o tarjeta inválida.") from None
    require_creation(recurring=True)
    merchant = settings.SUMUP_MERCHANT_CODE
    config = installation_config()
    normalized, amount, _ = validate_request(
        {field: data[field] for field in PAYMENT_FIELDS}, "subscription", config
    )
    fields = {
        "amount": amount,
        "currency": normalized["currency"],
        "description": normalized["description"],
        "return_url": normalized["return_url"],
        "merchant_code": merchant,
        "saved_card_id": card_id,
        "starts_on": starts,
        "interval_months": months,
        "interval_days": days,
    }
    with transaction.atomic():
        reference = normalized["reference"]
        subscription = Subscription.objects.filter(reference=reference).first()
        if subscription is None:
            if starts < timezone.localdate():
                raise PaymentError("invalid_schedule", "La fecha inicial ya pasó.")
            card = SavedCard.objects.filter(pk=card_id, merchant_code=merchant).first()
            if card is None:
                raise PaymentError("card_not_found", "Tarjeta guardada no encontrada.", 404)
            try:
                token_for(card)
            except ProviderError:
                raise PaymentError("card_not_ready", "Tarjeta sin verificar.", 409) from None
            if settings.SUMUP_SANDBOX_ONLY and not card.sandbox:
                raise PaymentError("card_not_ready", "Tarjeta fuera del entorno sandbox.", 409)
            if not settings.SUMUP_API_KEY:
                raise PaymentError("provider_not_configured", "Faltan credenciales de SumUp.", 503)
            subscription, created = Subscription.objects.get_or_create(
                reference=reference,
                defaults={**fields, "next_due_on": starts, "check_after": timezone.now()},
            )
            if created:
                audit(None, "subscription_created", subscription.id, reference)
                return subscription, True
        if any(getattr(subscription, key) != value for key, value in fields.items()):
            raise PaymentError("subscription_conflict", "La referencia tiene otros datos.", 409)
        return subscription, False


def serialize_subscription(subscription):
    latest = subscription.payments.order_by("-scheduled_for").first()
    return {
        "id": str(subscription.id),
        "reference": subscription.reference,
        "saved_card_id": str(subscription.saved_card_id),
        "amount": format(subscription.amount, ".2f"),
        "currency": subscription.currency,
        "merchant_code": subscription.merchant_code,
        "description": subscription.description,
        "return_url": subscription.return_url,
        "starts_on": iso(subscription.starts_on),
        **(
            {"interval_days": subscription.interval_days}
            if subscription.interval_days is not None
            else {"interval_months": subscription.interval_months}
        ),
        "next_due_on": iso(
            subscription.next_due_on if subscription.status == Subscription.Status.ACTIVE else None
        ),
        "status": subscription.status,
        "blocked_reason": subscription.blocked_reason or None,
        "latest_payment_id": str(latest.id) if latest else None,
        "created_at": iso(subscription.created_at),
        "cancelled_at": iso(subscription.cancelled_at),
    }


def cancel_subscription(subscription_id):
    with transaction.atomic():
        subscription = Subscription.objects.select_for_update().filter(pk=subscription_id).first()
        if subscription is None:
            raise PaymentError("not_found", "Suscripción no encontrada.", 404)
        if subscription.status != Subscription.Status.CANCELLED:
            current = subscription.payments.filter(scheduled_for=subscription.next_due_on).first()
            if current:
                request_payment_cancellation(current)
            subscription.status = Subscription.Status.CANCELLED
            subscription.cancelled_at = timezone.now()
            subscription.save(update_fields=["status", "cancelled_at"])
            audit(None, "subscription_cancelled", subscription.id, subscription.reference)
        return subscription


def next_occurrence(subscription, today):
    starts = subscription.starts_on
    if today < starts:
        return starts
    if subscription.interval_days is not None:
        step = subscription.interval_days
        periods = (today - starts).days // step + 1
        return starts + timedelta(days=periods * step)
    interval = subscription.interval_months
    elapsed = (today.year - starts.year) * 12 + today.month - starts.month
    months = starts.year * 12 + starts.month - 1 + (elapsed // interval) * interval
    year, month = divmod(months, 12)
    candidate = date(year, month + 1, min(starts.day, monthrange(year, month + 1)[1]))
    if candidate > today:
        return candidate
    year, month = divmod(months + interval, 12)
    return date(year, month + 1, min(starts.day, monthrange(year, month + 1)[1]))


def evaluate_subscription(subscription_id, config, now):
    today = timezone.localdate(now)
    with transaction.atomic():
        sub = Subscription.objects.select_for_update().get(pk=subscription_id)
        if (
            sub.status != Subscription.Status.ACTIVE
            or sub.next_due_on > today
            or sub.check_after > now
        ):
            return
        sub.check_after = now + timedelta(seconds=30)
        sub.save(update_fields=["check_after"])
        payment = sub.payments.select_for_update().filter(scheduled_for=sub.next_due_on).first()
        if payment:
            attempt = payment.attempts.get()
            if (
                attempt.status == Attempt.Status.PAID
                and not attempt.uncertain
                and attempt.closed_confirmed
                and attempt.transaction_id
                and attempt.paid_at
            ):
                sub.next_due_on = next_occurrence(sub, today)
                sub.save(update_fields=["next_due_on"])
                return
            if attempt.uncertain or not attempt.closed_confirmed:
                return
            reason = attempt.status
        else:
            reason = reserve_cycle(sub, config)
            if not reason:
                return
        sub.status = Subscription.Status.BLOCKED
        sub.blocked_reason = reason
        sub.save(update_fields=["status", "blocked_reason"])
        audit(None, "subscription_blocked", sub.id, reason)


def reserve_cycle(sub, config):
    if (
        sub.merchant_code != settings.SUMUP_MERCHANT_CODE
        or sub.currency not in config.currencies
        or sub.return_url not in config.allowed_return_urls
        or (settings.SUMUP_SANDBOX_ONLY and not sub.saved_card.sandbox)
    ):
        return "configuration_changed"
    if not settings.SUMUP_API_KEY:
        return "provider_not_configured"
    try:
        token_for(sub.saved_card)
    except (ProviderError, PaymentError):
        return "card_not_ready"
    reference = f"{SUBSCRIPTION_REFERENCE_PREFIX}{sub.id}:{sub.next_due_on.isoformat()}"
    data = {
        "reference": reference,
        "amount": format(sub.amount, ".0f" if sub.currency == "CLP" else ".2f"),
        "currency": sub.currency,
        "description": sub.description,
        "return_url": sub.return_url,
    }
    normalized, _, digest = validate_request(data, reference, config)
    payment = Payment.objects.create(
        reference=reference,
        amount=sub.amount,
        currency=sub.currency,
        merchant_code=sub.merchant_code,
        saved_card_id=sub.saved_card_id,
        subscription=sub,
        scheduled_for=sub.next_due_on,
    )
    reserve_attempt(payment, normalized, reference, digest, config)
    audit(None, "subscription_cycle_reserved", sub.id, reference)
    return ""
