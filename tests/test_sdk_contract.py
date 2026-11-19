import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from django.utils import timezone

from payments.gateway import ProviderError, SumUpGateway


def test_official_sdk_serializes_hosted_checkout_and_reference_lookup():
    attempt_id = uuid4()
    checkout = {
        "id": "provider-id",
        "checkout_reference": str(attempt_id),
        "amount": 19.95,
        "currency": "EUR",
        "merchant_code": "MTEST123",
        "status": "PENDING",
        "hosted_checkout_url": "https://checkout.sumup.com/pay/provider-id",
    }

    def receive(request):
        assert request.headers["Authorization"] == "Bearer test-sumup-key"
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["hosted_checkout"] == {"enabled": True}
            assert body["return_url"] == "https://payments.example.com/webhooks/sumup/"
            assert body["redirect_url"] == "https://saas.example.com/return"
            assert body["checkout_reference"] == str(attempt_id)
            assert body["valid_until"]
            assert Decimal(str(body["amount"])) == Decimal("19.95")
            return httpx.Response(201, json=checkout)
        assert request.url.params["checkout_reference"] == str(attempt_id)
        return httpx.Response(200, json=[checkout])

    with SumUpGateway() as gateway:
        gateway.client._client.close()
        gateway.client._client = httpx.Client(
            base_url="https://api.sumup.com",
            transport=httpx.MockTransport(receive),
            headers={"Authorization": "Bearer test-sumup-key"},
        )
        attempt = SimpleNamespace(
            id=attempt_id,
            payment=SimpleNamespace(
                amount=Decimal("19.95"), currency="EUR", merchant_code="MTEST123"
            ),
            description="Pedido",
            return_url="https://saas.example.com/return",
            expires_at=timezone.now(),
        )
        assert gateway.create(attempt)["id"] == "provider-id"
        assert gateway.find(str(attempt_id))[0]["id"] == "provider-id"


def test_sdk_errors_are_redacted():
    with SumUpGateway() as gateway:
        gateway.client._client.close()
        gateway.client._client = httpx.Client(
            base_url="https://api.sumup.com",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    401, json={"detail": "sensitive-data", "status": 401, "title": "Unauthorized"}
                )
            ),
        )
        with pytest.raises(ProviderError) as error:
            gateway.get("provider-id")
    assert "sensitive-data" not in str(error.value)


@pytest.mark.django_db
def test_scheduled_subscription_through_official_sdk(installation, payload, settings):
    from datetime import timedelta

    from payments.cards import refresh_card, start_card
    from payments.models import SavedCard
    from payments.subscriptions import create_subscription
    from payments.worker import worker_tick

    settings.SUMUP_SANDBOX_ONLY = True
    checkouts = {}
    transactions = {}
    requests = []
    customer = None

    def receive(request):
        nonlocal customer
        path = request.url.path
        requests.append((request.method, path))
        method = request.method
        if path == "/v1/merchants/MTEST123":
            return httpx.Response(
                200,
                json={
                    "merchant_code": "MTEST123",
                    "default_currency": "EUR",
                    "sandbox": True,
                    "country": "ES",
                    "default_locale": "es-ES",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            )
        if path == "/v0.1/customers" and method == "POST":
            customer = json.loads(request.content)["customer_id"]
            return httpx.Response(201, json={"customer_id": customer})
        if path.startswith("/v0.1/customers/") and not path.endswith("/payment-instruments"):
            return httpx.Response(404 if customer is None else 200, json={"customer_id": customer})
        if path == f"/v0.1/customers/{customer}/payment-instruments":
            return httpx.Response(
                200,
                json=[
                    {
                        "token": "mock-token",
                        "active": True,
                        "mandate": {
                            "type": "recurrent",
                            "status": "active",
                            "merchant_code": "MTEST123",
                        },
                        "card": {"last_4_digits": "0091"},
                    }
                ],
            )
        if path == "/v0.1/checkouts" and method == "POST":
            body = json.loads(request.content)
            assert body["customer_id"] == customer
            assert body["valid_until"]
            if body["purpose"] == "SETUP_RECURRING_PAYMENT":
                assert body["amount"] == 1
                checkout_id = "setup"
            else:
                assert body["purpose"] == "CHECKOUT"
                assert body["amount"] == 19.95 and "hosted_checkout" not in body
                checkout_id = "charge"
            checkouts[checkout_id] = {**body, "status": "PENDING", "id": checkout_id}
            return httpx.Response(201, json=checkouts[checkout_id])
        if path.startswith("/v0.1/checkouts/"):
            checkout_id = path.rsplit("/", 1)[-1]
            if method == "PUT":
                body = json.loads(request.content)
                assert body == {
                    "payment_type": "card",
                    "token": "mock-token",
                    "customer_id": customer,
                }
                assert checkout_id == "charge"
                mark_paid(checkout_id, "RECURRING")
            return httpx.Response(200, json=checkouts[checkout_id])
        if path == "/v2.1/merchants/MTEST123/transactions":
            return httpx.Response(200, json=transactions[request.url.params["id"]])
        raise AssertionError((method, path))

    def mark_paid(checkout_id, payment_type):
        checkout = checkouts[checkout_id]
        transaction_id = f"tx-{checkout_id}"
        checkout.update(
            status="PAID", transactions=[{"id": transaction_id, "status": "SUCCESSFUL"}]
        )
        transactions[transaction_id] = {
            "id": transaction_id,
            "payment_type": payment_type,
            "status": "SUCCESSFUL",
            "amount": checkout["amount"],
            "currency": "EUR",
            "merchant_code": "MTEST123",
        }

    with SumUpGateway() as gateway:
        gateway.client._client.close()
        gateway.client._client = httpx.Client(
            base_url="https://api.sumup.com", transport=httpx.MockTransport(receive)
        )
        card = start_card({"reference": "test-setup", "return_url": payload["return_url"]}, gateway)
        assert card.setup_checkout_id == "setup"
        mark_paid("setup", "ECOM")
        card = refresh_card(card.id, gateway)
        assert card.status == "ready" and card.setup_transaction_id == "tx-setup"
        subscription, _ = create_subscription(
            {
                **payload,
                "saved_card_id": str(card.id),
                "starts_on": timezone.localdate().isoformat(),
                "interval_months": 3,
            }
        )
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(204))) as sender:
            worker_tick(gateway, sender)
        attempt = subscription.payments.get().attempts.get()
        assert attempt.status == "paid" and attempt.transaction_id == "tx-charge"
        assert attempt.process_submitted_at < timezone.now() + timedelta(seconds=1)
        assert requests.count(("PUT", "/v0.1/checkouts/charge")) == 1
        assert SavedCard.objects.get(pk=card.id).token_ciphertext != "mock-token"
