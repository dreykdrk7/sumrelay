from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from payments.gateway import ProviderError, ProviderRejected
from payments.models import Attempt, AuditLog, Notification
from payments.services import cancel_attempt, create_payment, refresh_attempt
from payments.validation import PaymentError

pytestmark = pytest.mark.django_db


def test_creation_and_idempotent_replay(installation, payload, gateway):
    attempt, created = create_payment(payload, "key-1", gateway)
    replay, second = create_payment(payload, "key-1", gateway)
    assert created and not second and replay.pk == attempt.pk
    assert attempt.status == "pending" and not attempt.uncertain
    assert gateway.creates == 1
    assert Notification.objects.count() == 1


def test_key_reuse_with_other_payload_is_conflict(installation, payload, gateway):
    create_payment(payload, "key", gateway)
    with pytest.raises(PaymentError, match="otros datos"):
        create_payment({**payload, "amount": "20.00"}, "key", gateway)
    assert gateway.creates == 1


@pytest.mark.parametrize(
    "amount",
    ["0", "-1", "1.001", "NaN", "Infinity", "1e2", 19.95, True, "1000000000.00", " 1.00", "01.000"],
)
def test_invalid_amounts(installation, payload, gateway, amount):
    with pytest.raises(PaymentError) as error:
        create_payment({**payload, "amount": amount}, "key", gateway)
    assert error.value.code == "invalid_amount"
    assert gateway.creates == 0


def test_currency_and_return_validation(installation, payload, gateway):
    for update in [
        {"currency": "USD"},
        {"return_url": "https://evil.example.com"},
        {"extra": "unsupported"},
    ]:
        with pytest.raises(PaymentError):
            create_payment({**payload, **update}, "key", gateway)


def test_lost_create_response_recovers_without_new_checkout(installation, payload, gateway):
    gateway.create_error = ProviderError("Respuesta perdida")
    attempt, _ = create_payment(payload, "key", gateway)
    assert attempt.uncertain and attempt.sumup_id is None
    replay, _ = create_payment(payload, "key", gateway)
    assert replay.pk == attempt.pk and gateway.creates == 1
    with pytest.raises(PaymentError) as error:
        create_payment(payload, "other-key", gateway)
    assert error.value.code == "attempt_blocked" and gateway.creates == 1
    resolved = refresh_attempt(attempt.id, gateway)
    assert not resolved.uncertain and resolved.sumup_id


def test_missing_checkout_after_ambiguous_creation_stays_blocked(installation, payload, gateway):
    gateway.create_error = ProviderError("Timeout")
    attempt, _ = create_payment(payload, "key", gateway)
    gateway.checkouts.clear()
    assert refresh_attempt(attempt.id, gateway).uncertain
    with pytest.raises(PaymentError):
        create_payment(payload, "key-2", gateway)
    assert gateway.creates == 1


def test_pending_or_failed_checkout_blocks_another_attempt(installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    for state in ["PENDING", "FAILED"]:
        gateway.checkouts[attempt.sumup_id]["status"] = state
        with pytest.raises(PaymentError) as error:
            create_payment(payload, f"key-{state}", gateway)
        assert error.value.code == "attempt_blocked"
    assert gateway.creates == 1


def test_confirmed_expiry_allows_new_attempt_and_keeps_snapshot(installation, payload, gateway):
    original, _ = create_payment(payload, "key", gateway)
    gateway.checkouts[original.sumup_id]["status"] = "EXPIRED"
    second, _ = create_payment(payload, "key-2", gateway)
    assert second.payment_id == original.payment_id and second.id != original.id
    assert gateway.creates == 2
    gateway.checkouts[second.sumup_id]["status"] = "EXPIRED"
    with pytest.raises(PaymentError) as error:
        create_payment({**payload, "amount": "29.95"}, "key-3", gateway)
    assert error.value.code == "order_conflict"


def test_local_expiry_is_not_proof_of_provider_expiry(installation, payload, gateway):
    from datetime import timedelta

    from django.utils import timezone

    attempt, _ = create_payment(payload, "key", gateway)
    Attempt.objects.filter(pk=attempt.pk).update(expires_at=timezone.now() - timedelta(days=1))
    with pytest.raises(PaymentError):
        create_payment(payload, "key-2", gateway)


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", 1),
        ("currency", "USD"),
        ("merchant_code", "MOTHER"),
        ("checkout_reference", "some-other-attempt"),
    ],
)
def test_checkout_identity_mismatch_never_confirms_payment(
    installation, payload, gateway, field, value
):
    attempt, _ = create_payment(payload, "key", gateway)
    gateway.pay(attempt)
    gateway.checkouts[attempt.sumup_id][field] = value
    result = refresh_attempt(attempt.id, gateway)
    assert result.uncertain and result.paid_at is None and result.status == "pending"


def test_transaction_identity_mismatch_never_confirms_payment(installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    gateway.pay(attempt)["merchant_code"] = "MOTHER"
    result = refresh_attempt(attempt.id, gateway)
    assert result.uncertain and result.paid_at is None


def test_payment_and_duplicate_checks_emit_one_change(installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    gateway.pay(attempt)
    paid = refresh_attempt(attempt.id, gateway)
    refresh_attempt(attempt.id, gateway)
    assert paid.status == "paid" and paid.paid_at and not paid.uncertain
    assert Notification.objects.count() == 2
    with pytest.raises(PaymentError):
        create_payment(payload, "key-2", gateway)


def test_partial_and_multiple_refunds_are_deduplicated(installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    detail = gateway.pay(attempt)
    refresh_attempt(attempt.id, gateway)
    detail["status"] = "REFUNDED"
    detail["events"] = [{"id": 1, "type": "REFUND", "amount": 5.00, "status": "REFUNDED"}]
    first = refresh_attempt(attempt.id, gateway)
    assert first.status == "partially_refunded" and first.refunded_amount == Decimal("5.00")
    detail["transaction_events"] = [
        {"id": 1, "event_type": "REFUND", "amount": 5.00, "status": "REFUNDED"}
    ]
    refresh_attempt(attempt.id, gateway)
    assert Notification.objects.count() == 3
    detail["events"].append({"id": 2, "type": "REFUND", "amount": 14.95, "status": "SUCCESSFUL"})
    full = refresh_attempt(attempt.id, gateway)
    assert full.status == "refunded" and full.refunded_amount == Decimal("19.95") and full.paid_at
    assert full.refund_events.count() == 2
    with pytest.raises(PaymentError):
        create_payment(payload, "key-2", gateway)


def test_pending_or_failed_refunds_do_not_reduce_original_payment(installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    detail = gateway.pay(attempt)
    detail["events"] = [
        {"id": 1, "type": "REFUND", "amount": 5, "status": "PENDING"},
        {"id": 2, "type": "REFUND", "amount": 2, "status": "FAILED"},
    ]
    result = refresh_attempt(attempt.id, gateway)
    assert result.status == "paid" and result.refunded_amount == 0
    assert result.refund_events.count() == 2


def test_refund_without_amounts_is_uncertain(installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    detail = gateway.pay(attempt)
    refresh_attempt(attempt.id, gateway)
    detail["status"] = "REFUNDED"
    result = refresh_attempt(attempt.id, gateway)
    assert result.uncertain and result.status == "paid" and result.refunded_amount == 0


def test_provider_outage_preserves_paid_state_then_recovers(installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    gateway.pay(attempt)
    refresh_attempt(attempt.id, gateway)
    gateway.get_error = ProviderError("Servicio no disponible")
    result = refresh_attempt(attempt.id, gateway)
    assert result.status == "paid" and result.uncertain
    gateway.get_error = None
    assert not refresh_attempt(attempt.id, gateway).uncertain


def test_cancellation_is_verified_and_audited(installation, payload, gateway):
    actor = get_user_model().objects.create_user("admin")
    attempt, _ = create_payment(payload, "key", gateway)
    result = cancel_attempt(attempt.id, actor, gateway)
    assert result.status == "cancelled" and result.closed_confirmed and not result.uncertain
    assert AuditLog.objects.filter(actor=actor).count() == 2


def test_payment_during_cancellation_wins(installation, payload, gateway):
    actor = get_user_model().objects.create_user("admin")
    attempt, _ = create_payment(payload, "key", gateway)
    gateway.on_deactivate = lambda _: gateway.pay(attempt)
    result = cancel_attempt(attempt.id, actor, gateway)
    assert result.status == "paid" and not result.uncertain
    with pytest.raises(PaymentError):
        create_payment(payload, "key-2", gateway)


def test_cancel_timeout_remains_uncertain(installation, payload, gateway):
    actor = get_user_model().objects.create_user("admin")
    attempt, _ = create_payment(payload, "key", gateway)
    gateway.deactivate_error = ProviderError("Respuesta perdida")
    result = cancel_attempt(attempt.id, actor, gateway)
    assert result.uncertain and not result.closed_confirmed


def test_definite_creation_rejection_allows_explicit_new_attempt(installation, payload, gateway):
    original_create = gateway.create

    def reject(_):
        raise ProviderRejected("Solicitud rechazada")

    gateway.create = reject
    first, _ = create_payment(payload, "key", gateway)
    assert first.status == "failed" and first.closed_confirmed and not first.uncertain
    gateway.create = original_create
    second, _ = create_payment(payload, "key-2", gateway)
    assert second.payment_id == first.payment_id and second.pk != first.pk


def test_transaction_lookup_rejection_cannot_close_created_checkout(installation, payload, gateway):
    original_create = gateway.create

    def create_paid(attempt):
        data = original_create(attempt)
        attempt.sumup_id = data["id"]
        gateway.pay(attempt)
        return gateway.get(data["id"])

    def rejected_lookup(*_):
        raise ProviderRejected("Permisos insuficientes para consultar transacción")

    gateway.create = create_paid
    gateway.transaction = rejected_lookup
    result, _ = create_payment(payload, "key", gateway)
    assert result.uncertain and not result.closed_confirmed and result.sumup_id
    with pytest.raises(PaymentError):
        create_payment(payload, "key-2", gateway)


def test_late_payment_on_older_attempt_is_visible_and_blocks_checkout(
    installation, payload, gateway
):
    from payments.serializers import serialize_payment

    first, _ = create_payment(payload, "key", gateway)
    gateway.checkouts[first.sumup_id]["status"] = "EXPIRED"
    second, _ = create_payment(payload, "key-2", gateway)
    # Defensive handling if provider finality assumptions fail in a near-simultaneous race.
    gateway.pay(first)
    refresh_attempt(first.id, gateway)
    result = serialize_payment(first.payment)
    assert result["status"] == "paid" and result["uncertain"] and result["checkout_url"] is None
    assert result["latest_attempt_id"] == str(second.pk)
    gateway.checkouts[second.sumup_id]["status"] = "EXPIRED"
    refresh_attempt(second.id, gateway)
    assert not serialize_payment(first.payment)["uncertain"]
