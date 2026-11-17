import json
from unittest.mock import patch

import pytest
from django.conf import settings
from django.utils import timezone

from payments.models import Attempt, Notification
from payments.services import create_payment
from payments.worker import worker_tick

pytestmark = pytest.mark.django_db


def auth():
    return {"HTTP_AUTHORIZATION": f"Bearer {settings.SAAS_API_KEYS[0]}"}


def test_server_authentication_is_required(client):
    for headers in [{}, {"HTTP_AUTHORIZATION": "Bearer incorrect"}]:
        assert client.get("/api/v1/payments/?reference=order", **headers).status_code == 401


def test_create_and_lookup_by_id_and_reference(client, installation, payload, gateway):
    with patch("payments.services.SumUpGateway") as factory:
        factory.return_value.__enter__.return_value = gateway
        response = client.post(
            "/api/v1/payments/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="key",
            **auth(),
        )
    assert response.status_code == 201
    data = response.json()
    assert data["reference"] == payload["reference"] and data["amount"] == "19.95"
    assert data["merchant_code"] == settings.SUMUP_MERCHANT_CODE
    assert data["requested_attempt"]["id"] == data["latest_attempt_id"]
    assert response["Cache-Control"] == "no-store"
    assert client.get(response["Location"], **auth()).json()["id"] == data["id"]
    assert client.get("/api/v1/payments/?reference=ORDER-42", **auth()).json()["id"] == data["id"]


def test_invalid_json_and_missing_idempotency(client, installation, payload):
    response = client.post("/api/v1/payments/", data="{", content_type="application/json", **auth())
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_json"
    response = client.post(
        "/api/v1/payments/", data=json.dumps(payload), content_type="application/json", **auth()
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"


def test_unconfigured_installation_returns_503(client, payload):
    response = client.post(
        "/api/v1/payments/",
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="key",
        **auth(),
    )
    assert response.status_code == 503


def test_browser_return_is_not_a_confirmation(client, installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    assert (
        client.get(f"/api/v1/payments/{attempt.payment_id}/?status=PAID", **auth()).json()["status"]
        == "pending"
    )
    attempt.refresh_from_db()
    assert not attempt.paid_at


def test_duplicate_callbacks_queue_one_verified_check(client, installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    body = {"id": attempt.sumup_id, "event_type": "CHECKOUT_STATUS_CHANGED", "status": "PAID"}
    for _ in range(2):
        response = client.post(
            "/webhooks/sumup/", data=json.dumps(body), content_type="application/json"
        )
        assert response.status_code == 204 and not response.content
    attempt.refresh_from_db()
    assert attempt.check_requested_at and attempt.status == "pending"
    gateway.pay(attempt)
    import httpx

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(204))) as sender:
        worker_tick(gateway, sender)
    attempt.refresh_from_db()
    assert attempt.status == "paid" and attempt.check_requested_at is None
    assert Notification.objects.count() == 2


def test_unknown_callbacks_are_ignored(client):
    for body in [
        {"event_type": "NEW_EVENT"},
        {"event_type": "CHECKOUT_STATUS_CHANGED", "id": "unknown-checkout"},
    ]:
        assert (
            client.post(
                "/webhooks/sumup/", data=json.dumps(body), content_type="application/json"
            ).status_code
            == 204
        )


def test_webhook_arriving_during_verification_is_not_lost(installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    old_get = gateway.get

    def get_and_signal(checkout_id):
        Attempt.objects.filter(pk=attempt.pk).update(check_requested_at=timezone.now())
        return old_get(checkout_id)

    gateway.get = get_and_signal
    from payments.services import refresh_attempt

    refresh_attempt(attempt.id, gateway)
    attempt.refresh_from_db()
    assert attempt.check_requested_at is not None
