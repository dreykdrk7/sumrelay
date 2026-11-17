import hashlib
import hmac
from datetime import timedelta

import httpx
import pytest
from django.conf import settings
from django.utils import timezone

from payments.models import Notification, ReconciliationRun
from payments.services import create_payment
from payments.worker import deliver_notification, worker_tick

pytestmark = pytest.mark.django_db


def test_signed_notification_and_success(installation, payload, gateway):
    create_payment(payload, "key", gateway)
    notification = Notification.objects.get()

    def receive(request):
        stamp = request.headers["X-Webhook-Timestamp"]
        signature = hmac.new(
            settings.SAAS_WEBHOOK_SECRET.encode(),
            stamp.encode() + b"." + request.content,
            hashlib.sha256,
        ).hexdigest()
        assert request.headers["X-Webhook-Signature"] == f"v1={signature}"
        assert request.headers["X-Webhook-Id"] == str(notification.id)
        assert request.url == installation.notification_url
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(receive)) as client:
        deliver_notification(notification.id, client)
        deliver_notification(notification.id, client)
    notification.refresh_from_db()
    assert notification.delivered_at and notification.attempts == 1


def test_failed_delivery_retries_with_stable_body_and_id(installation, payload, gateway):
    create_payment(payload, "key", gateway)
    notification = Notification.objects.get()
    sent = []

    def receive(request):
        sent.append((request.headers["X-Webhook-Id"], request.content))
        return httpx.Response(503 if len(sent) == 1 else 200)

    with httpx.Client(transport=httpx.MockTransport(receive)) as client:
        deliver_notification(notification.id, client)
        notification.refresh_from_db()
        assert not notification.delivered_at and notification.next_attempt_at > timezone.now()
        Notification.objects.filter(pk=notification.pk).update(next_attempt_at=timezone.now())
        deliver_notification(notification.id, client)
    assert sent[0] == sent[1]
    notification.refresh_from_db()
    assert notification.delivered_at and notification.attempts == 2


def test_timeout_does_not_leak_remote_details(installation, payload, gateway):
    create_payment(payload, "key", gateway)
    notification = Notification.objects.get()

    def receive(request):
        raise httpx.ConnectError("secret-token", request=request)

    with httpx.Client(transport=httpx.MockTransport(receive)) as client:
        deliver_notification(notification.id, client)
    notification.refresh_from_db()
    assert "secret-token" not in notification.last_error


def test_retry_window_is_24_hours(installation, payload, gateway):
    create_payment(payload, "key", gateway)
    notification = Notification.objects.get()
    assert 86390 < (notification.deadline_at - notification.created_at).total_seconds() <= 86400
    Notification.objects.filter(pk=notification.pk).update(
        deadline_at=timezone.now() - timedelta(seconds=1)
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("Must not send"))
    ) as client:
        deliver_notification(notification.id, client)
    notification.refresh_from_db()
    assert notification.exhausted and notification.attempts == 0


def test_reconciliation_detects_refund_without_callback(installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    detail = gateway.pay(attempt)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(204))) as client:
        worker_tick(gateway, client)
        detail["status"] = "REFUNDED"
        detail["events"] = [{"id": 10, "type": "REFUND", "amount": 19.95, "status": "REFUNDED"}]
        installation.reconcile_requested = True
        installation.save()
        worker_tick(gateway, client)
    attempt.refresh_from_db()
    assert attempt.status == "refunded"
    assert ReconciliationRun.objects.filter(success=True).count() == 2


def test_redirect_does_not_count_as_delivery(installation, payload, gateway):
    create_payment(payload, "key", gateway)
    notification = Notification.objects.get()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"Location": "https://other.example.com"})
        )
    ) as client:
        deliver_notification(notification.id, client)
    notification.refresh_from_db()
    assert not notification.delivered_at and "302" in notification.last_error
