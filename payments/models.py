import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

SUPPORTED_CURRENCIES = {
    "BRL": 2,
    "CHF": 2,
    "CLP": 0,
    "COP": 2,
    "CZK": 2,
    "DKK": 2,
    "EUR": 2,
    "GBP": 2,
    "HRK": 2,
    "HUF": 2,
    "NOK": 2,
    "PLN": 2,
    "RON": 2,
    "SEK": 2,
    "USD": 2,
}


def default_returns():
    return []


def default_currencies():
    return ["EUR"]


class IntegrationConfig(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    notification_url = models.URLField("URL de notificaciones al SaaS", max_length=2048)
    allowed_return_urls = models.JSONField(
        "Retornos permitidos (URLs exactas)", default=default_returns
    )
    currencies = models.JSONField("Monedas habilitadas", default=default_currencies)
    reconciliation_seconds = models.PositiveIntegerField(
        "Intervalo de conciliación (s)", default=900
    )
    checkout_ttl_seconds = models.PositiveIntegerField("Caducidad solicitada (s)", default=1800)
    reconcile_requested = models.BooleanField(
        "Conciliación manual pendiente", default=False, editable=False
    )

    class Meta:
        verbose_name = "configuración de la instalación"
        verbose_name_plural = "configuración de la instalación"

    def clean(self):
        from .validation import validate_url

        if self.pk != 1:
            raise ValidationError("Solo se permite una configuración por instalación.")
        validate_url(self.notification_url)
        if not isinstance(self.allowed_return_urls, list) or not self.allowed_return_urls:
            raise ValidationError("Configura al menos una URL de retorno.")
        for url in self.allowed_return_urls:
            validate_url(url)
        if (
            not isinstance(self.currencies, list)
            or not self.currencies
            or any(not isinstance(c, str) or c not in SUPPORTED_CURRENCIES for c in self.currencies)
        ):
            raise ValidationError("Lista de monedas inválida.")
        if self.reconciliation_seconds < 60 or not 60 <= self.checkout_ttl_seconds <= 1800:
            raise ValidationError("Conciliación mínima: 60 s. Caducidad: entre 60 y 1800 s.")

    def __str__(self):
        return "Configuración de esta instalación"


class SavedCard(models.Model):
    """One consented setup, scoped to this installation. Never serialize token_ciphertext."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=120, unique=True)
    merchant_code = models.CharField(max_length=64)
    return_url = models.URLField(max_length=2048)
    setup_checkout_id = models.CharField(max_length=128, unique=True, null=True)
    setup_transaction_id = models.CharField(max_length=128, blank=True, db_index=True)
    create_submitted_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=16, default="pending")
    uncertain = models.BooleanField(default=True)
    token_ciphertext = models.TextField(blank=True, editable=False)
    last4 = models.CharField(max_length=4, blank=True)
    sandbox = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    checked_at = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def customer_id(self):
        return f"sumrelay-{self.id}"


class Subscription(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Activa"
        BLOCKED = "blocked", "Bloqueada"
        CANCELLED = "cancelled", "Cancelada"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=120, unique=True)
    saved_card = models.ForeignKey(SavedCard, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=11, decimal_places=2)
    currency = models.CharField(max_length=3)
    merchant_code = models.CharField(max_length=64)
    description = models.CharField(max_length=500)
    return_url = models.URLField(max_length=2048)
    starts_on = models.DateField()
    interval_months = models.PositiveSmallIntegerField()
    next_due_on = models.DateField()
    check_after = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    blocked_reason = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=["status", "check_after", "next_due_on"])]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="subscription_positive"),
            models.CheckConstraint(
                condition=Q(interval_months__range=(1, 12)), name="subscription_interval"
            ),
        ]

    def __str__(self):
        return self.reference


class Payment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField("Referencia del pedido", max_length=120, unique=True)
    amount = models.DecimalField("Importe", max_digits=11, decimal_places=2)
    currency = models.CharField("Moneda", max_length=3)
    merchant_code = models.CharField("Comercio destinatario", max_length=64)
    saved_card = models.ForeignKey(SavedCard, on_delete=models.PROTECT, null=True, blank=True)
    subscription = models.ForeignKey(
        Subscription, on_delete=models.PROTECT, related_name="payments", null=True, blank=True
    )
    scheduled_for = models.DateField(null=True, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_positive"),
            models.UniqueConstraint(
                fields=["subscription", "scheduled_for"], name="unique_subscription_cycle"
            ),
            models.CheckConstraint(
                condition=Q(subscription__isnull=True, scheduled_for__isnull=True)
                | Q(subscription__isnull=False, scheduled_for__isnull=False),
                name="payment_subscription_schedule",
            ),
        ]
        verbose_name = "pago"

    def __str__(self):
        return self.reference


class Attempt(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        PAID = "paid", "Pagado"
        FAILED = "failed", "Fallido"
        EXPIRED = "expired", "Expirado"
        CANCELLED = "cancelled", "Cancelado"
        PARTIALLY_REFUNDED = "partially_refunded", "Parcialmente devuelto"
        REFUNDED = "refunded", "Devuelto"
        DISPUTED = "disputed", "En disputa"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="attempts")
    idempotency_key = models.CharField(max_length=128, unique=True)
    request_hash = models.CharField(max_length=64)
    description = models.CharField(max_length=500)
    return_url = models.URLField(max_length=2048)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    uncertain = models.BooleanField("Estado incierto", default=True)
    closed_confirmed = models.BooleanField("Checkout no cobrable confirmado", default=False)
    sumup_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    checkout_url = models.URLField(max_length=2048, blank=True)
    transaction_id = models.CharField(max_length=128, blank=True, db_index=True)
    transaction_code = models.CharField(max_length=128, blank=True)
    create_submitted_at = models.DateTimeField(null=True, blank=True)
    process_submitted_at = models.DateTimeField(null=True, blank=True)
    action_required = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)
    refunded_amount = models.DecimalField(max_digits=11, decimal_places=2, default=Decimal("0"))
    dispute_detected_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=250, blank=True)
    check_requested_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "intento"
        constraints = [
            models.CheckConstraint(condition=Q(refunded_amount__gte=0), name="refund_nonnegative"),
        ]

    def __str__(self):
        return f"{self.payment.reference} · {self.id}"


class RefundEvent(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.PROTECT, related_name="refund_events")
    provider_id = models.CharField(max_length=128)
    amount = models.DecimalField(max_digits=11, decimal_places=2)
    status = models.CharField(max_length=32)
    occurred_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["attempt", "provider_id"], name="unique_refund_event")
        ]
        verbose_name = "evento de devolución"
        verbose_name_plural = "eventos de devolución"


class VerifiedTransaction(models.Model):
    transaction_id = models.CharField(max_length=128, primary_key=True)
    checkout_id = models.CharField(max_length=128, unique=True)


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attempt = models.ForeignKey(Attempt, on_delete=models.PROTECT, related_name="notifications")
    payload = models.JSONField()
    target_url = models.URLField(max_length=2048)
    created_at = models.DateTimeField(auto_now_add=True)
    deadline_at = models.DateTimeField()
    next_attempt_at = models.DateTimeField(db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    exhausted = models.BooleanField(default=False, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "notificación al SaaS"
        verbose_name_plural = "notificaciones al SaaS"


class ReconciliationRun(models.Model):
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    success = models.BooleanField(default=False)
    checked = models.PositiveIntegerField(default=0)
    errors = models.PositiveIntegerField(default=0)
    last_attempt_id = models.UUIDField(null=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "conciliación"
        verbose_name_plural = "conciliaciones"


class WorkerHeartbeat(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    checked_at = models.DateTimeField()


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    actor_name = models.CharField(max_length=150)
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=128)
    outcome = models.CharField(max_length=250)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "registro de auditoría"
        verbose_name_plural = "registros de auditoría"
