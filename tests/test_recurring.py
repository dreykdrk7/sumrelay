import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from payments.cards import (
    cancel_recurring_order,
    cipher,
    process_recurring,
    refresh_card,
    serialize_card,
    start_card,
    token_for,
)
from payments.gateway import ProviderError
from payments.models import Attempt, SavedCard
from payments.services import create_payment, refresh_attempt
from payments.validation import PaymentError
from tests.conftest import FakeGateway


class RecurringGateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.processes = 0
        self.decline = False
        self.instrument_active = True

    def account(self, merchant_code):
        return {"merchant_code": merchant_code, "currency": "EUR", "sandbox": True}

    def ensure_customer(self, card):
        pass

    def create_card_setup(self, card):
        self.creates += 1
        data = {
            "id": f"setup-{card.id}",
            "checkout_reference": str(card.id),
            "amount": 1,
            "currency": "EUR",
            "merchant_code": card.merchant_code,
            "customer_id": card.customer_id,
            "status": "PENDING",
        }
        self.checkouts[data["id"]] = data
        if self.create_error:
            raise self.create_error
        return data.copy()

    def instruments(self, customer_id):
        return [
            {
                "token": "fake-reusable-token",
                "active": self.instrument_active,
                "mandate": {
                    "merchant_code": settings.SUMUP_MERCHANT_CODE,
                    "status": "active",
                    "type": "recurrent",
                },
                "card": {"last_4_digits": "0091"},
            }
        ]

    def create(self, attempt):
        data = super().create(attempt)
        if attempt.payment.saved_card_id:
            data["customer_id"] = attempt.payment.saved_card.customer_id
            self.checkouts[data["id"]]["customer_id"] = data["customer_id"]
        return data

    def process_card(self, attempt, token):
        assert token == "fake-reusable-token"
        self.processes += 1
        if self.decline:
            self.checkouts[attempt.sumup_id]["status"] = "FAILED"
            return {"status": "FAILED"}
        tx = self.pay(attempt)
        tx["payment_type"] = "RECURRING"
        return {"status": "PAID"}


@pytest.fixture
def card(installation):
    return SavedCard.objects.create(
        reference="sub-test",
        merchant_code=settings.SUMUP_MERCHANT_CODE,
        return_url=installation.allowed_return_urls[0],
        status="ready",
        uncertain=False,
        token_ciphertext=cipher().encrypt(b"fake-reusable-token").decode(),
        expires_at=timezone.now() + timedelta(minutes=30),
    )


@pytest.mark.django_db
def test_token_is_encrypted_and_never_serialized(card):
    assert card.token_ciphertext != "fake-reusable-token"
    assert token_for(card) == "fake-reusable-token"
    result = json.dumps(serialize_card(card))
    assert "fake-reusable-token" not in result
    assert card.token_ciphertext not in result


@pytest.mark.django_db
def test_card_and_cancellation_tombstone_match_the_published_contract(card, payload):
    from payments.serializers import serialize_payment
    from tests.test_contract import validate_contract

    validate_contract("CardSetup", serialize_card(card))
    validate_contract("CreateRecurringPayment", {**payload, "saved_card_id": str(card.id)})
    payment = cancel_recurring_order({**payload, "saved_card_id": str(card.id)}, RecurringGateway())
    validate_contract("Payment", serialize_payment(payment))


@pytest.mark.django_db
def test_setup_recovery_does_not_post_twice(installation):
    gateway = RecurringGateway()
    gateway.create_error = ProviderError("Timeout")
    data = {"reference": "sub-test", "return_url": installation.allowed_return_urls[0]}
    card = start_card(data, gateway)
    assert card.uncertain
    recovered = start_card(data, gateway)
    assert recovered.id == card.id
    assert recovered.setup_checkout_id
    assert gateway.creates == 1


@pytest.mark.django_db
def test_ready_requires_verified_customer_transaction_and_active_mandate(installation):
    gateway = RecurringGateway()
    card = start_card(
        {"reference": "sub-test", "return_url": installation.allowed_return_urls[0]}, gateway
    )
    gateway.checkouts[card.setup_checkout_id].update(status="PAID", transaction_id="setup-tx")
    gateway.transactions["setup-tx"] = {
        "id": "setup-tx",
        "amount": 1,
        "currency": "EUR",
        "merchant_code": card.merchant_code,
        "status": "SUCCESSFUL",
    }
    gateway.instrument_active = False
    card = refresh_card(card.id, gateway)
    assert card.uncertain and not card.token_ciphertext
    gateway.instrument_active = True
    card = refresh_card(card.id, gateway)
    assert card.status == "ready" and not card.uncertain


@pytest.mark.django_db
def test_recurring_is_processed_once_and_reconciled(card, payload):
    gateway = RecurringGateway()
    attempt, _ = create_payment(payload, "recurring-1", gateway, saved_card_id=card.id)
    assert not attempt.uncertain
    paid = process_recurring(attempt.id, gateway)
    assert paid.status == "paid"
    assert paid.closed_confirmed
    process_recurring(attempt.id, gateway)
    assert gateway.processes == 1
    assert Attempt.objects.count() == 1


@pytest.mark.django_db
def test_uncertain_processing_is_not_repeated_even_after_restart(card, payload):
    gateway = RecurringGateway()
    attempt, _ = create_payment(payload, "recurring-1", gateway, saved_card_id=card.id)
    Attempt.objects.filter(pk=attempt.id).update(process_submitted_at=timezone.now())
    process_recurring(attempt.id, gateway)
    assert gateway.processes == 0


@pytest.mark.django_db
def test_decline_is_closed_before_another_attempt(card, payload):
    gateway = RecurringGateway()
    gateway.decline = True
    attempt, _ = create_payment(payload, "recurring-1", gateway, saved_card_id=card.id)
    closed = process_recurring(attempt.id, gateway)
    assert closed.status == "cancelled"
    assert closed.closed_confirmed and not closed.uncertain
    assert gateway.deactivations == 1
    second, _ = create_payment(payload, "recurring-2", gateway, saved_card_id=card.id)
    assert second.payment_id == closed.payment_id
    assert second.id != closed.id


@pytest.mark.django_db
def test_cancel_marker_blocks_dispatch_and_new_retry(card, payload):
    gateway = RecurringGateway()
    attempt, _ = create_payment(payload, "recurring-1", gateway, saved_card_id=card.id)
    attempt.payment.cancel_requested_at = timezone.now()
    attempt.payment.save()
    process_recurring(attempt.id, gateway)
    assert gateway.processes == 0
    gateway.deactivate(attempt.sumup_id)
    refresh_attempt(attempt.id, gateway)
    with pytest.raises(PaymentError, match="cancelado"):
        create_payment(payload, "recurring-2", gateway, saved_card_id=card.id)


@pytest.mark.django_db
def test_invalid_card_is_not_a_one_time_payment(installation, payload):
    with pytest.raises(PaymentError):
        create_payment(payload, "recurring-1", RecurringGateway(), saved_card_id="invalid")


@pytest.mark.django_db
def test_cancel_before_delayed_create_blocks_provider_post(card, payload):
    gateway = RecurringGateway()
    payment = cancel_recurring_order({**payload, "saved_card_id": str(card.id)}, gateway)
    assert payment.cancel_requested_at
    assert not payment.attempts.exists()
    with pytest.raises(PaymentError, match="cancelado"):
        create_payment(payload, "late-arrival-1", gateway, saved_card_id=card.id)
    assert gateway.creates == 0


@pytest.mark.django_db
def test_cancel_after_reservation_blocks_the_not_yet_sent_create(card, payload):
    from unittest.mock import patch

    from payments.services import _create_checkout

    gateway = RecurringGateway()
    with patch("payments.services._create_checkout"):
        attempt, _ = create_payment(
            payload, "reserved-before-cancel", gateway, saved_card_id=card.id
        )
    cancel_recurring_order({**payload, "saved_card_id": str(card.id)}, gateway)
    _create_checkout(attempt, gateway)
    result = refresh_attempt(attempt.id, gateway)
    assert result.status == "cancelled" and result.closed_confirmed and not result.uncertain
    assert gateway.creates == 0 and gateway.processes == 0


@pytest.mark.django_db
def test_recurring_endpoints_require_server_auth(client):
    for path in (
        "/api/v1/card-setups/",
        "/api/v1/recurring-payments/",
        "/api/v1/recurring-payments/cancel/",
    ):
        assert client.post(path, data="{}", content_type="application/json").status_code == 401
    assert not SavedCard.objects.exists()


@pytest.mark.django_db
def test_null_card_id_is_rejected_before_creating_any_payment(client, installation, payload):
    response = client.post(
        "/api/v1/recurring-payments/",
        data=json.dumps({**payload, "saved_card_id": None}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer " + settings.SAAS_API_KEYS[0],
        HTTP_IDEMPOTENCY_KEY="bad-card-1",
    )
    assert response.status_code == 400
    assert not Attempt.objects.exists()


@pytest.mark.django_db
def test_card_snapshot_preserves_recorded_state_without_gateway(client, card, monkeypatch):
    from uuid import uuid4

    from tests.test_contract import auth, validate_contract

    def unexpected_gateway(*args, **kwargs):
        pytest.fail("A snapshot must not open the provider adapter")

    monkeypatch.setattr("payments.cards.SumUpGateway", unexpected_gateway)
    path = f"/api/v1/card-setups/{card.pk}/snapshot/"
    assert client.get(path).status_code == 401
    assert client.get(f"/api/v1/card-setups/{uuid4()}/snapshot/", **auth()).status_code == 404
    before = serialize_card(card)
    result = client.get(path, **auth())
    assert result.status_code == 200
    validate_contract("CardSetup", result.json())
    assert result.json() == before
    card.refresh_from_db()
    assert serialize_card(card) == before
