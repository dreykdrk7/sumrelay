import hashlib
import hmac
import json
from contextlib import contextmanager
from datetime import timedelta

import httpx
from django.conf import settings
from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from .cards import process_recurring
from .gateway import SumUpGateway
from .models import (
    Attempt,
    IntegrationConfig,
    Notification,
    ReconciliationRun,
    Subscription,
    WorkerHeartbeat,
)
from .services import _create_checkout, installation_config, refresh_attempt
from .subscriptions import evaluate_subscription


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


def reconcile(gateway, run, *, limit):
    attempts = Attempt.objects.filter(created_at__lte=run.started_at).order_by("pk")
    if run.last_attempt_id:
        attempts = attempts.filter(pk__gt=run.last_attempt_id)
    batch = list(attempts.values_list("id", flat=True)[:limit])
    for attempt_id in batch:
        attempt = refresh_attempt(attempt_id, gateway)
        run.checked += 1
        run.errors += int(attempt.uncertain)
        run.last_attempt_id = attempt_id
        run.save(update_fields=["checked", "errors", "last_attempt_id"])
    if len(batch) < limit:
        run.finished_at = timezone.now()
        run.success = run.errors == 0
        run.save(update_fields=["finished_at", "success"])


def worker_tick(gateway=None, client=None):
    with worker_lock() as acquired:
        if not acquired:
            return
        config = installation_config()
        if gateway is None:
            with SumUpGateway() as provider:
                run_work(config, provider, client)
        else:
            run_work(config, gateway, client)
        WorkerHeartbeat.objects.update_or_create(pk=1, defaults={"checked_at": timezone.now()})


def run_work(config, gateway, client):
    now = timezone.now()
    if settings.PAYMENT_CREATION_ENABLED and settings.RECURRING_CHARGES_ENABLED:
        subscriptions = Subscription.objects.filter(
            status=Subscription.Status.ACTIVE,
            next_due_on__lte=timezone.localdate(now),
            check_after__lte=now,
        ).order_by("check_after", "id")
        for subscription_id in list(subscriptions.values_list("id", flat=True)[:50]):
            evaluate_subscription(subscription_id, config, now)

    if settings.PAYMENT_CREATION_ENABLED and settings.SUMUP_API_KEY:
        unsent = Attempt.objects.filter(
            create_submitted_at__isnull=True,
            sumup_id__isnull=True,
            closed_confirmed=False,
            payment__merchant_code=settings.SUMUP_MERCHANT_CODE,
        ).select_related("payment")
        if not settings.RECURRING_CHARGES_ENABLED:
            unsent = unsent.filter(payment__saved_card__isnull=True)
        for attempt in list(unsent[:3]):
            _create_checkout(attempt, gateway)

    due = Attempt.objects.filter(
        Q(check_requested_at__isnull=False)
        | (
            Q(paid_at__isnull=True, closed_confirmed=False, create_submitted_at__isnull=False)
            & (
                Q(last_checked_at__isnull=True)
                | Q(last_checked_at__lte=now - timedelta(seconds=settings.PENDING_POLL_SECONDS))
            )
        )
    ).order_by(F("last_checked_at").asc(nulls_first=True), "created_at")
    for attempt_id in list(due.values_list("id", flat=True)[:5]):
        refresh_attempt(attempt_id, gateway)

    latest = ReconciliationRun.objects.first()
    if latest is None or (
        latest.finished_at
        and (
            config.reconcile_requested
            or latest.finished_at <= now - timedelta(seconds=config.reconciliation_seconds)
        )
    ):
        # Clear before starting so an admin request during the run is preserved.
        IntegrationConfig.objects.filter(pk=1).update(reconcile_requested=False)
        latest = ReconciliationRun.objects.create()
    if latest.finished_at is None:
        reconcile(gateway, latest, limit=5)

    if settings.PAYMENT_CREATION_ENABLED and settings.RECURRING_CHARGES_ENABLED:
        ready = Attempt.objects.filter(
            payment__saved_card__isnull=False,
            payment__saved_card__status="ready",
            payment__saved_card__uncertain=False,
            payment__cancel_requested_at__isnull=True,
            payment__merchant_code=settings.SUMUP_MERCHANT_CODE,
            process_submitted_at__isnull=True,
            uncertain=False,
            status=Attempt.Status.PENDING,
            sumup_id__isnull=False,
            expires_at__gt=timezone.now(),
        )
        for attempt_id in list(ready.values_list("id", flat=True)[:3]):
            process_recurring(attempt_id, gateway)

    pending = Notification.objects.filter(
        delivered_at__isnull=True, exhausted=False, next_attempt_at__lte=timezone.now()
    ).order_by("next_attempt_at", "created_at")
    for notification_id in list(pending.values_list("id", flat=True)[:10]):
        deliver_notification(notification_id, client)
