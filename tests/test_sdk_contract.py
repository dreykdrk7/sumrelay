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
