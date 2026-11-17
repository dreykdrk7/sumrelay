import hashlib
import hmac
import json
from contextlib import contextmanager
from datetime import timedelta

import httpx
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .models import Attempt, IntegrationConfig, Notification, ReconciliationRun
from .services import installation_config, refresh_attempt


@contextmanager
def worker_lock():
    """Session lock shared by worker instances. Never hold it on a pooled connection."""
    if connection.vendor != "postgresql":
        yield True  # SQLite is only supported for isolated unit tests.
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(734891025)")
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(734891025)")


def deliver_notification(notification_id, client=None):
    if client is None:
        with httpx.Client(
            timeout=settings.WEBHOOK_TIMEOUT, follow_redirects=False, trust_env=False
        ) as sender:
            return deliver_notification(notification_id, sender)
    with transaction.atomic():
        notification = Notification.objects.select_for_update().get(pk=notification_id)
        now = timezone.now()
        if (
            notification.delivered_at
            or notification.exhausted
            or notification.next_attempt_at > now
        ):
            return
        if now >= notification.deadline_at:
            notification.exhausted = True
            notification.last_error = "Agotado el plazo de 24 horas. Reintento manual disponible."
            notification.save()
            return
        body = json.dumps(
            notification.payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True
        ).encode()
        timestamp = str(int(now.timestamp()))
        signature = hmac.new(
            settings.SAAS_WEBHOOK_SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        notification.attempts += 1
        try:
            response = client.post(
                notification.target_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Id": str(notification.id),
                    "X-Webhook-Timestamp": timestamp,
                    "X-Webhook-Signature": f"v1={signature}",
                },
            )
            if 200 <= response.status_code < 300:
                notification.delivered_at = now
                notification.last_error = ""
            else:
                notification.last_error = f"Respuesta HTTP {response.status_code} del SaaS."
        except httpx.HTTPError:
            notification.last_error = "No se pudo entregar la notificación al SaaS."
        if not notification.delivered_at:
            delays = [60, 300, 900, 3600]
            delay = delays[min(notification.attempts - 1, len(delays) - 1)]
            notification.next_attempt_at = min(
                now + timedelta(seconds=delay), notification.deadline_at
            )
        notification.save()


def reconcile(gateway=None):
    run = ReconciliationRun.objects.create()
    # V1 deliberately scans known checkouts, avoiding unvalidated changes_since semantics.
    for attempt_id in (
        Attempt.objects.order_by("created_at").values_list("id", flat=True).iterator()
    ):
        attempt = refresh_attempt(attempt_id, gateway)
        run.checked += 1
        run.errors += int(attempt.uncertain)
    run.finished_at = timezone.now()
    run.success = run.errors == 0
    run.save()
    return run


def worker_tick(gateway=None, client=None):
    with worker_lock() as acquired:
        if not acquired:
            return
        config = installation_config()
        latest = ReconciliationRun.objects.order_by("-started_at").first()
        now = timezone.now()
        if (
            config.reconcile_requested
            or latest is None
            or latest.started_at <= now - timedelta(seconds=config.reconciliation_seconds)
        ):
            # Clear before starting so an admin request during the run is preserved.
            IntegrationConfig.objects.filter(pk=1).update(reconcile_requested=False)
            reconcile(gateway)
        else:
            due = Attempt.objects.filter(
                Q(check_requested_at__isnull=False)
                | (
                    Q(paid_at__isnull=True, closed_confirmed=False)
                    & (
                        Q(last_checked_at__isnull=True)
                        | Q(
                            last_checked_at__lte=now
                            - timedelta(seconds=settings.PENDING_POLL_SECONDS)
                        )
                    )
                )
            ).values_list("id", flat=True)
            for attempt_id in due.iterator():
                refresh_attempt(attempt_id, gateway)
        pending = Notification.objects.filter(
            delivered_at__isnull=True,
            exhausted=False,
            next_attempt_at__lte=timezone.now(),
        ).values_list("id", flat=True)
        for notification_id in pending.iterator():
            deliver_notification(notification_id, client)
        from .cards import process_recurring

        ready = Attempt.objects.filter(
            payment__saved_card__isnull=False,
            process_submitted_at__isnull=True,
            uncertain=False,
            status=Attempt.Status.PENDING,
            sumup_id__isnull=False,
        ).values_list("id", flat=True)
        for attempt_id in ready.iterator():
            process_recurring(attempt_id, gateway)
