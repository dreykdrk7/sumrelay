from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from payments.models import Attempt, AuditLog, IntegrationConfig, Notification
from payments.services import create_payment

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client):
    user = get_user_model().objects.create_superuser(
        "operator", "op@example.com", "strong-test-pass"
    )
    client.force_login(user)
    return client


def test_admin_requires_active_staff(client, installation):
    assert client.get("/admin/").status_code == 302
    user = get_user_model().objects.create_user("not-staff", password="strong-test-pass")
    client.force_login(user)
    assert client.get("/admin/").status_code == 302
    user.is_staff = True
    user.is_active = False
    user.save()
    assert client.get("/admin/").status_code == 302


def test_panel_pages_and_search_render(admin_client, installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    for page in [
        "",
        "payment/",
        f"payment/{attempt.payment_id}/change/",
        "attempt/",
        f"attempt/{attempt.pk}/change/",
        "notification/",
        "integrationconfig/",
        "reconciliationrun/",
        "auditlog/",
        "payment/?q=ORDER-42",
    ]:
        path = "/admin/" if not page else f"/admin/payments/{page}"
        assert admin_client.get(path).status_code == 200


def test_manual_check_action_is_audited(admin_client, installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    response = admin_client.post(
        "/admin/payments/attempt/",
        {
            "action": "request_checks",
            "_selected_action": [str(attempt.pk)],
        },
    )
    assert response.status_code == 302
    attempt.refresh_from_db()
    assert attempt.check_requested_at
    assert AuditLog.objects.get().actor_name == "operator"


def test_manual_notification_retry_preserves_event_id(admin_client, installation, payload, gateway):
    create_payment(payload, "key", gateway)
    notification = Notification.objects.get()
    Notification.objects.filter(pk=notification.pk).update(
        exhausted=True, deadline_at=timezone.now() - timedelta(days=1)
    )
    response = admin_client.post(
        "/admin/payments/notification/",
        {
            "action": "retry_notifications",
            "_selected_action": [str(notification.pk)],
        },
    )
    assert response.status_code == 302
    notification.refresh_from_db()
    assert not notification.exhausted and notification.deadline_at > timezone.now()
    assert notification.payload["id"] == str(notification.id)
    assert AuditLog.objects.get().action == "notification_retry"


def test_manual_reconciliation_is_queued(admin_client, installation):
    admin_client.post(
        "/admin/payments/integrationconfig/",
        {
            "action": "request_reconciliation",
            "_selected_action": ["1"],
        },
    )
    assert IntegrationConfig.objects.get().reconcile_requested
    assert AuditLog.objects.get().action == "reconciliation_requested"


def test_financial_fields_cannot_be_edited_in_admin(admin_client, installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    admin_client.post(f"/admin/payments/attempt/{attempt.pk}/change/", {"status": "paid"})
    attempt.refresh_from_db()
    assert attempt.status == Attempt.Status.PENDING and attempt.paid_at is None
