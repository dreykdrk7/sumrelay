from copy import deepcopy

import pytest
from django.conf import settings

from payments.models import IntegrationConfig


@pytest.fixture
def installation(db):
    return IntegrationConfig.objects.create(
        notification_url="https://saas.example.com/webhooks/payments",
        allowed_return_urls=["https://saas.example.com/payment-result"],
    )


@pytest.fixture
def payload():
    return {
        "reference": "ORDER-42",
        "amount": "19.95",
        "currency": "EUR",
        "description": "Pedido 42",
        "return_url": "https://saas.example.com/payment-result",
    }


class FakeGateway:
    def __init__(self):
        self.checkouts = {}
        self.transactions = {}
        self.creates = 0
        self.gets = 0
        self.deactivations = 0
        self.create_error = None
        self.get_error = None
        self.deactivate_error = None
        self.on_deactivate = None

    def create(self, attempt):
        self.creates += 1
        data = {
            "id": f"checkout-{attempt.id}",
            "checkout_reference": str(attempt.id),
            "amount": float(attempt.payment.amount),
            "currency": attempt.payment.currency,
            "merchant_code": attempt.payment.merchant_code,
            "status": "PENDING",
            "hosted_checkout_url": f"https://checkout.sumup.com/pay/{attempt.id}",
            "transactions": [],
        }
        self.checkouts[data["id"]] = data
        if self.create_error:
            raise self.create_error
        return deepcopy(data)

    def get(self, checkout_id):
        self.gets += 1
        if self.get_error:
            raise self.get_error
        return deepcopy(self.checkouts[checkout_id])

    def find(self, reference):
        return [
            deepcopy(c) for c in self.checkouts.values() if c["checkout_reference"] == reference
        ]

    def deactivate(self, checkout_id):
        self.deactivations += 1
        if self.deactivate_error:
            raise self.deactivate_error
        if self.on_deactivate:
            self.on_deactivate(checkout_id)
        else:
            self.checkouts[checkout_id]["status"] = "EXPIRED"
        return self.get(checkout_id)

    def transaction(self, merchant_code, transaction_id):
        assert merchant_code == settings.SUMUP_MERCHANT_CODE
        return deepcopy(self.transactions[transaction_id])

    def pay(self, attempt):
        tx_id = f"tx-{attempt.id}"
        self.checkouts[attempt.sumup_id].update(
            status="PAID",
            transactions=[{"id": tx_id, "status": "SUCCESSFUL"}],
        )
        self.transactions[tx_id] = {
            "id": tx_id,
            "amount": float(attempt.payment.amount),
            "currency": attempt.payment.currency,
            "merchant_code": attempt.payment.merchant_code,
            "transaction_code": "TXCODE",
            "status": "SUCCESSFUL",
            "events": [],
        }
        return self.transactions[tx_id]


@pytest.fixture
def gateway():
    return FakeGateway()
