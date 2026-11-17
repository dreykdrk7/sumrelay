"""Deployment controls: reading, reconciliation and cancellation remain available."""

from django.conf import settings

from .validation import PaymentError


def require_creation(*, recurring=False):
    if not settings.PAYMENT_CREATION_ENABLED:
        raise PaymentError("payments_disabled", "La creación de cobros está desactivada.", 503)
    if recurring and not settings.RECURRING_CHARGES_ENABLED:
        raise PaymentError("recurring_disabled", "Los cobros periódicos están desactivados.", 503)
