import json
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from payments.cards import start_card
from payments.models import Attempt, Payment, SavedCard
from payments.services import create_payment, refresh_attempt
from payments.validation import PaymentError
from payments.worker import worker_tick
from tests.test_api import auth
from tests.test_recurring import RecurringGateway
from tests.test_recurring import card as card

pytestmark = pytest.mark.django_db


def test_disabled_api_never_reserves_or_contacts_provider(client, installation, payload, settings):
    settings.PAYMENT_CREATION_ENABLED = False
    for endpoint, body in [
        ("payments", payload),
        ("card-setups", {"reference": "card", "return_url": payload["return_url"]}),
        ("recurring-payments", {**payload, "saved_card_id": str(uuid4())}),
    ]:
        with (
            patch("payments.services.SumUpGateway") as service,
            patch("payments.cards.SumUpGateway") as cards,
        ):
            response = client.post(
                f"/api/v1/{endpoint}/",
                data=json.dumps(body),
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY="paused-key",
                **auth(),
            )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "payments_disabled"
        service.assert_not_called()
        cards.assert_not_called()
    assert Payment.objects.count() == Attempt.objects.count() == SavedCard.objects.count() == 0


def test_recurring_can_be_disabled_independently(installation, payload, settings):
    gateway = RecurringGateway()
    settings.RECURRING_CHARGES_ENABLED = False
    attempt, _ = create_payment(payload, "one-off-still-enabled", gateway)
    assert attempt.sumup_id and gateway.creates == 1
    with pytest.raises(PaymentError, match="periódicos"):
        start_card({"reference": "card", "return_url": payload["return_url"]}, gateway)
    assert gateway.creates == 1 and SavedCard.objects.count() == 0


@pytest.mark.parametrize("creation,recurring", [(False, True), (True, False), (False, False)])
def test_paused_worker_reconciles_without_charging(card, payload, settings, creation, recurring):
    gateway = RecurringGateway()
    attempt, _ = create_payment(payload, "reserved", gateway, saved_card_id=card.id)
    settings.PAYMENT_CREATION_ENABLED = creation
    settings.RECURRING_CHARGES_ENABLED = recurring
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(204))) as sender:
        worker_tick(gateway, sender)
    attempt.refresh_from_db()
    assert gateway.processes == 0 and attempt.process_submitted_at is None
    gateway.pay(attempt)["payment_type"] = "RECURRING"
    assert refresh_attempt(attempt.id, gateway).status == "paid"
