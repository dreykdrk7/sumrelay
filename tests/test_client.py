import hashlib
import hmac
import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from django.conf import settings

from clients.python.payment_client import (
    InvalidWebhook,
    PaymentMismatch,
    PaymentNotReady,
    ProcessorClient,
    ProcessorError,
    checkout_url_for_redirect,
    confirm_for_order,
    verify_webhook,
)
from payments.serializers import serialize_payment
from payments.services import create_payment, refresh_attempt
from payments.worker import worker_tick


@pytest.fixture
def paid_snapshot(installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    gateway.pay(attempt)
    refresh_attempt(attempt.pk, gateway)
    return serialize_payment(attempt.payment)


def expected_order(snapshot):
    return {
        "payment_id": snapshot["id"],
        "reference": "ORDER-42",
        "amount": Decimal("19.95"),
        "currency": "EUR",
        "merchant_code": settings.SUMUP_MERCHANT_CODE,
    }


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, {"interval_days": 30}),
        ({"interval_days": 30}, {"interval_days": 30}),
        ({"interval_months": 3}, {"interval_months": 3}),
    ],
)
def test_subscription_client_sends_one_explicit_calendar(options, expected):
    def receive(request):
        data = json.loads(request.content)
        assert {k: v for k, v in data.items() if k.startswith("interval_")} == expected
        return httpx.Response(201, json={"id": str(uuid4())})

    with ProcessorClient(
        "http://localhost:8000", settings.SAAS_API_KEYS[0], transport=httpx.MockTransport(receive)
    ) as client:
        fields = dict(
            reference="PLAN",
            amount=Decimal("22.99"),
            currency="EUR",
            description="Plan",
            return_url="https://example.invalid/return",
            starts_on=date(2026, 1, 31),
            saved_card_id=str(uuid4()),
        )
        client.create_subscription(**fields, **options)
        with pytest.raises(ValueError):
            client.create_subscription(**fields, interval_months=1, interval_days=30)


def test_client_sends_server_key_decimal_and_idempotency_without_browser_data():
    def receive(request):
        assert request.method == "POST" and request.url.path == "/api/v1/payments/"
        assert request.headers["Authorization"] == f"Bearer {settings.SAAS_API_KEYS[0]}"
        assert request.headers["Idempotency-Key"] == "order-42-attempt-1"
        assert json.loads(request.content)["amount"] == "19.95"
        assert settings.SAAS_API_KEYS[0].encode() not in request.content
        return httpx.Response(202, json={"id": str(uuid4()), "uncertain": True})

    with ProcessorClient(
        "http://localhost:8000", settings.SAAS_API_KEYS[0], transport=httpx.MockTransport(receive)
    ) as client:
        result = client.create_payment(
            reference="ORDER-42",
            amount=Decimal("19.95"),
            currency="EUR",
            description="Pedido 42",
            return_url="https://app.example.com/return",
            idempotency_key="order-42-attempt-1",
        )
    assert result["uncertain"]


def test_timeout_is_not_retried_automatically_or_logged_with_secrets():
    calls = []

    def receive(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive-provider-details", request=request)

    with ProcessorClient(
        "https://payments.example.com",
        settings.SAAS_API_KEYS[0],
        transport=httpx.MockTransport(receive),
    ) as client:
        with pytest.raises(ProcessorError) as error:
            client.create_payment(
                reference="ORDER-42",
                amount=Decimal("19.95"),
                currency="EUR",
                description="Pedido",
                return_url="https://app.example.com",
                idempotency_key="persisted-key",
            )
    assert len(calls) == 1 and error.value.code == "connection_uncertain"
    assert "sensitive-provider-details" not in str(error.value)
    assert settings.SAAS_API_KEYS[0] not in str(error.value)


def test_reference_is_encoded_as_query_data():
    reference = "ORDER/42?extra=1&return=✓"

    def receive(request):
        assert dict(request.url.params) == {"reference": reference}
        return httpx.Response(200, json={"id": str(uuid4())})

    with ProcessorClient(
        "https://payments.example.com",
        settings.SAAS_API_KEYS[0],
        transport=httpx.MockTransport(receive),
    ) as client:
        client.get_payment_by_reference(reference)


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com?token=secret",
        "https://example.com/api",
    ],
)
def test_client_rejects_unsafe_or_ambiguous_origins(origin):
    with pytest.raises(ValueError):
        ProcessorClient(origin, settings.SAAS_API_KEYS[0])


def test_redirect_is_not_followed_with_the_server_key():
    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example.com"})

    with ProcessorClient(
        "https://payments.example.com",
        settings.SAAS_API_KEYS[0],
        transport=httpx.MockTransport(receive),
    ) as client:
        with pytest.raises(ProcessorError) as error:
            client.get_payment(str(uuid4()))
    assert error.value.status == 302 and len(calls) == 1


def test_conflict_code_is_preserved_without_exposing_remote_message():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            409,
            json={"error": {"code": "attempt_blocked", "message": "sensitive-provider-details"}},
        )
    )
    with ProcessorClient(
        "https://payments.example.com", settings.SAAS_API_KEYS[0], transport=transport
    ) as client:
        with pytest.raises(ProcessorError) as error:
            client.get_payment(str(uuid4()))
    assert error.value.code == "attempt_blocked" and error.value.status == 409
    assert "sensitive-provider-details" not in str(error.value)


@pytest.mark.django_db
def test_paid_payment_matches_original_order(paid_snapshot):
    confirmed = confirm_for_order(paid_snapshot, **expected_order(paid_snapshot))
    assert confirmed.amount == Decimal("19.95") and confirmed.transaction_id


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("reference", "ANOTHER-ORDER"),
        ("amount", "1.00"),
        ("currency", "USD"),
        ("merchant_code", "MOTHER"),
    ],
)
def test_fulfilment_rejects_financial_mismatches(paid_snapshot, field, value):
    changed = deepcopy(paid_snapshot)
    changed[field] = value
    with pytest.raises(PaymentMismatch):
        confirm_for_order(changed, **expected_order(paid_snapshot))


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state", ["pending", "failed", "expired", "cancelled", "partially_refunded", "refunded"]
)
def test_fulfilment_requires_a_current_paid_state(paid_snapshot, state):
    changed = {**paid_snapshot, "status": state}
    with pytest.raises(PaymentNotReady):
        confirm_for_order(changed, **expected_order(paid_snapshot))


@pytest.mark.django_db
def test_fulfilment_rejects_uncertainty_refunds_or_open_siblings(paid_snapshot):
    for changed in [
        {**paid_snapshot, "uncertain": True},
        {**paid_snapshot, "refunded_amount": "1.00"},
        {
            **paid_snapshot,
            "attempts": paid_snapshot["attempts"]
            + [{"status": "pending", "uncertain": False, "closed_confirmed": False}],
        },
    ]:
        with pytest.raises(PaymentNotReady):
            confirm_for_order(changed, **expected_order(paid_snapshot))


def signed_event(secret=None, timestamp="1800000000", body=None):
    body = (
        body or json.dumps({"id": str(uuid4()), "type": "payment.updated", "payment": {}}).encode()
    )
    event_id = json.loads(body)["id"]
    signature = hmac.new(
        (secret or settings.SAAS_WEBHOOK_SECRET).encode(),
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return body, {
        "X-Webhook-Id": event_id,
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": "v1=" + signature,
    }


def test_webhook_verification_supports_case_insensitive_headers_and_secret_rotation():
    body, headers = signed_event()
    headers = {k.lower(): v for k, v in headers.items()}
    event = verify_webhook(
        body,
        headers,
        ["another-secret-long-enough-for-rotation", settings.SAAS_WEBHOOK_SECRET],
        now=1800000000,
    )
    assert event["id"] == headers["x-webhook-id"]


@pytest.mark.parametrize("change", ["body", "signature", "timestamp", "event_id"])
def test_webhook_verification_rejects_tampering(change):
    body, headers = signed_event()
    if change == "body":
        body += b" "
    elif change == "signature":
        headers["X-Webhook-Signature"] = "v1=" + "0" * 64
    elif change == "timestamp":
        headers["X-Webhook-Timestamp"] = "1799999999"
    else:
        headers["X-Webhook-Id"] = str(uuid4())
    with pytest.raises(InvalidWebhook):
        verify_webhook(body, headers, settings.SAAS_WEBHOOK_SECRET, now=1800000000)


@pytest.mark.parametrize("clock", [1799999699, 1800000301])
def test_stale_or_future_webhook_is_rejected(clock):
    body, headers = signed_event()
    with pytest.raises(InvalidWebhook):
        verify_webhook(body, headers, settings.SAAS_WEBHOOK_SECRET, now=clock)


@pytest.mark.django_db
def test_consumer_verifies_actual_worker_notifications(installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    gateway.pay(attempt)
    received = []

    def receive(request):
        received.append(
            verify_webhook(request.content, request.headers, settings.SAAS_WEBHOOK_SECRET)
        )
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(receive)) as sender:
        worker_tick(gateway, sender)
    paid = next(e for e in received if e["payment"]["status"] == "paid")
    confirmed = confirm_for_order(paid["payment"], **expected_order(paid["payment"]))
    assert confirmed.payment_id == str(attempt.payment_id)
    assert len({e["id"] for e in received}) == len(received)


@pytest.mark.django_db
def test_checkout_helper_only_returns_the_current_open_checkout(installation, payload, gateway):
    first, _ = create_payment(payload, "key", gateway)
    payment = serialize_payment(first.payment)
    assert checkout_url_for_redirect(payment) == first.checkout_url
    assert checkout_url_for_redirect({**payment, "uncertain": True}) is None
    assert checkout_url_for_redirect({**payment, "status": "paid"}) is None
    gateway.checkouts[first.sumup_id]["status"] = "EXPIRED"
    second, _ = create_payment(payload, "key-2", gateway)
    latest = serialize_payment(second.payment)
    assert checkout_url_for_redirect(latest) == second.checkout_url
    first.refresh_from_db()
    from payments.serializers import serialize_attempt

    assert (
        checkout_url_for_redirect({**latest, "requested_attempt": serialize_attempt(first)}) is None
    )
    latest["attempts"][-1]["checkout_url"] = "https://untrusted.example.com/"
    assert checkout_url_for_redirect(latest) is None


def test_client_handles_integer_currency_from_a_django_decimal_field():
    def receive(request):
        assert json.loads(request.content)["amount"] == "1000"
        return httpx.Response(201, json={"id": str(uuid4())})

    with ProcessorClient(
        "https://payments.example.com",
        settings.SAAS_API_KEYS[0],
        transport=httpx.MockTransport(receive),
    ) as client:
        client.create_payment(
            reference="ORDER-CLP",
            amount=Decimal("1000.00"),
            currency="CLP",
            description="Pedido",
            return_url="https://app.example.com",
            idempotency_key="key",
        )


@pytest.mark.django_db
def test_django_consumer_can_create_replay_and_confirm_through_http_api(
    client, installation, payload, gateway
):
    from payments.models import Attempt

    def serve(request):
        response = client.generic(
            request.method,
            request.url.raw_path.decode(),
            data=request.content,
            content_type="application/json",
            HTTP_AUTHORIZATION=request.headers.get("Authorization", ""),
            HTTP_IDEMPOTENCY_KEY=request.headers.get("Idempotency-Key", ""),
        )
        return httpx.Response(response.status_code, json=response.json())

    with (
        patch("payments.services.SumUpGateway") as factory,
        ProcessorClient(
            "http://localhost:8000", settings.SAAS_API_KEYS[0], transport=httpx.MockTransport(serve)
        ) as processor,
    ):
        factory.return_value.__enter__.return_value = gateway
        request = {**payload, "amount": Decimal(payload["amount"]), "idempotency_key": "key"}
        first = processor.create_payment(**request)
        repeated = processor.create_payment(**request)
        assert first["requested_attempt"]["id"] == repeated["requested_attempt"]["id"]
        assert gateway.creates == 1 and checkout_url_for_redirect(first)
        attempt = Attempt.objects.get(pk=first["requested_attempt"]["id"])
        gateway.pay(attempt)
        refresh_attempt(attempt.pk, gateway)
        current = processor.get_payment(first["id"])
        confirmed = confirm_for_order(current, **expected_order(current))
        assert confirmed.reference == payload["reference"]
        assert checkout_url_for_redirect(current) is None
