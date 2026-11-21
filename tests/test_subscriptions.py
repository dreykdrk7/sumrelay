import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from datetime import timezone as utc_timezone
from threading import Barrier

import httpx
import pytest
from django.conf import settings
from django.db import connection, connections
from django.utils import timezone

from clients.python.payment_client import confirm_for_order, verify_webhook
from payments.cards import process_recurring
from payments.gateway import ProviderError
from payments.models import Attempt, AuditLog, Notification, ReconciliationRun, Subscription
from payments.services import create_payment, refresh_attempt
from payments.subscriptions import (
    cancel_subscription,
    create_subscription,
    evaluate_subscription,
    next_occurrence,
)
from payments.worker import worker_tick
from tests.test_contract import auth, validate_contract
from tests.test_recurring import RecurringGateway, card  # noqa: F401


@pytest.fixture
def clock(monkeypatch):
    times = [datetime(2026, 1, 31, 10, tzinfo=utc_timezone.utc)]
    monkeypatch.setattr(timezone, "now", lambda: times[0])
    return times


@pytest.fixture
def plan(payload, card, clock):  # noqa: F811
    return {
        **payload,
        "saved_card_id": str(card.id),
        "starts_on": clock[0].date().isoformat(),
        "interval_months": 1,
    }


def post(client, path, body):
    return client.post(path, json.dumps(body), content_type="application/json", **auth())


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("cadence", "span", "second", "third"),
    [
        ({"interval_months": 1}, 28, "2026-02-28", "2026-03-31"),
        ({"interval_days": 30}, 30, "2026-03-02", "2026-04-01"),
    ],
)
def test_api_worker_renewals_and_signed_notifications(
    client, installation, plan, clock, cadence, span, second, third
):
    plan.pop("interval_months")
    plan.update(cadence)
    url = "/api/v1/subscriptions/"
    expected = client.post(url, "{}", content_type="application/json").status_code
    assert expected == 401
    validate_contract("CreateSubscription", plan)
    response = post(client, url, plan)
    assert response.status_code == 201
    detail = response["Location"]
    subscription_id = response.json()["id"]
    validate_contract("Subscription", response.json())
    assert post(client, url, {**plan, "amount": "19.950"}).status_code == 400
    assert post(client, url, {**plan, "amount": "21.00"}).status_code == 409
    assert post(client, url, plan).status_code == 200

    gateway = RecurringGateway()
    events = []

    def receive(request):
        event = verify_webhook(
            request.content,
            request.headers,
            settings.SAAS_WEBHOOK_SECRET,
            now=clock[0].timestamp(),
        )
        validate_contract("PaymentUpdated", event)
        events.append(event)
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(receive)) as sender:
        worker_tick(gateway, sender)
        clock[0] += timedelta(seconds=31)
        worker_tick(gateway, sender)
        assert client.get(detail, **auth()).json()["next_due_on"] == second
        clock[0] += timedelta(days=span - 1)
        worker_tick(gateway, sender)
        assert gateway.creates == 1
        clock[0] += timedelta(days=1)
        worker_tick(gateway, sender)
        clock[0] += timedelta(seconds=31)
        worker_tick(gateway, sender)

    assert gateway.creates == gateway.processes == 2
    page = client.get(detail + "payments/", **auth()).json()
    validate_contract("SubscriptionPayments", page)
    assert [p["scheduled_for"] for p in page["payments"]] == [second, "2026-01-31"]
    assert len(client.get(detail + f"payments/?before={second}", **auth()).json()["payments"]) == 1
    assert client.get(detail + "payments/?before=x", **auth()).status_code == 400
    assert client.get(detail, **auth()).json()["next_due_on"] == third
    assert post(client, url, plan).json()["id"] == subscription_id
    assert not Notification.objects.filter(delivered_at__isnull=True).exists()
    paid = [event["payment"] for event in events if event["payment"]["status"] == "paid"]
    for payment in paid:
        assert payment["subscription_id"] == subscription_id
        assert confirm_for_order(
            payment,
            payment_id=payment["id"],
            reference=payment["reference"],
            amount=Subscription.objects.get().amount,
            currency=plan["currency"],
            merchant_code=settings.SUMUP_MERCHANT_CODE,
        ).transaction_id
    assert len(paid) == 2

    for _ in range(2):
        result = client.post(detail + "cancel/", **auth())
        assert result.status_code == 200
        assert result.json()["status"] == "cancelled"
    assert AuditLog.objects.filter(action="subscription_cancelled").count() == 1


@pytest.mark.django_db
def test_fixed_interval_contract_and_idempotency(client, installation, plan):
    url = "/api/v1/subscriptions/"
    original = dict(plan)
    plan.pop("interval_months")
    for interval in [
        {},
        {"interval_days": True},
        {"interval_days": 31},
        {"interval_days": None},
        {"interval_days": 30, "interval_months": 1},
    ]:
        assert post(client, url, {**plan, **interval}).status_code == 400
    assert not Subscription.objects.exists()
    fixed = {**plan, "interval_days": 30}
    assert post(client, url, fixed).status_code == 201
    assert post(client, url, fixed).status_code == 200
    assert post(client, url, original).status_code == 409
    assert Subscription.objects.count() == 1


@pytest.mark.parametrize(
    ("start", "today", "expected"),
    [
        (date(2028, 1, 31), date(2028, 1, 31), date(2028, 3, 1)),
        (date(2026, 3, 1), date(2026, 3, 1), date(2026, 3, 31)),
        (date(2026, 10, 1), date(2026, 10, 1), date(2026, 10, 31)),
        (date(2026, 1, 31), date(2026, 4, 6), date(2026, 5, 1)),
    ],
)
def test_fixed_calendar_uses_dates_and_skips_overdue_cycles(start, today, expected):
    sub = Subscription(starts_on=start, interval_days=30)
    assert next_occurrence(sub, today) == expected


@pytest.mark.django_db
def test_scheduled_checkout_recovers_lost_create_without_second_post(installation, plan, clock):
    class LostCreateGateway(RecurringGateway):
        def create(self, attempt):
            super().create(attempt)
            raise ProviderError("No response")

    sub, _ = create_subscription(plan)
    evaluate_subscription(sub.id, installation, clock[0])
    attempt = Attempt.objects.get()
    assert attempt.create_submitted_at is None
    gateway = LostCreateGateway()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(204))) as sender:
        for _ in range(3):
            worker_tick(gateway, sender)
            clock[0] += timedelta(seconds=61)
    assert gateway.creates == gateway.processes == 1
    attempt.refresh_from_db()
    assert attempt.status == "paid"


@pytest.mark.django_db
def test_ambiguous_put_blocks_future_periods_and_is_never_replayed(installation, plan, clock):
    class LostProcessGateway(RecurringGateway):
        def process_card(self, attempt, token):
            self.processes += 1
            raise ProviderError("Lost response")

    gateway = LostProcessGateway()
    gateway.deactivate_error = ProviderError("Unresolved cancellation")
    sub, _ = create_subscription(plan)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(204))) as sender:
        worker_tick(gateway, sender)
        clock[0] += timedelta(days=61)
        worker_tick(gateway, sender)
        assert sub.payments.count() == gateway.creates == gateway.processes == 1
        assert Attempt.objects.get().uncertain
        gateway.deactivate_error = None
        clock[0] += timedelta(seconds=61)
        worker_tick(gateway, sender)
        clock[0] += timedelta(seconds=61)
        worker_tick(gateway, sender)
    sub.refresh_from_db()
    assert sub.status == "blocked"
    assert sub.blocked_reason == "cancelled"
    assert sub.payments.count() == 1


@pytest.mark.django_db
def test_cancelled_subscription_never_dispatches_reserved_attempt(installation, plan, clock):
    sub, _ = create_subscription(plan)
    evaluate_subscription(sub.id, installation, clock[0])
    cancel_subscription(sub.id)
    gateway = RecurringGateway()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(204))) as sender:
        worker_tick(gateway, sender)
    attempt = Attempt.objects.get()
    assert gateway.creates == gateway.processes == 0
    assert attempt.closed_confirmed and not attempt.uncertain and attempt.status == "cancelled"
    assert attempt.payment.cancel_requested_at


@pytest.mark.django_db
def test_subscription_with_action_required_sends_signed_state_and_never_replays(
    installation, plan, clock
):
    class ChallengeGateway(RecurringGateway):
        def process_card(self, attempt, token):
            self.processes += 1
            return {"next_step": {"payload": {"sensitive": "challenge-value"}}}

    gateway = ChallengeGateway()
    create_subscription(plan)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(204))) as sender:
        worker_tick(gateway, sender)
        clock[0] += timedelta(minutes=2)
        worker_tick(gateway, sender)
    attempt = Attempt.objects.get()
    assert attempt.action_required and not attempt.paid_at
    assert gateway.processes == 1
    outcomes = [event.payload for event in Notification.objects.all()]
    assert any(event["payment"]["attempts"][0]["action_required"] for event in outcomes)
    assert "challenge-value" not in json.dumps(outcomes)


@pytest.mark.django_db
def test_paused_worker_exposes_status_and_does_not_reserve(client, installation, plan, settings):
    sub, _ = create_subscription(plan)
    url = "/api/v1/operations/status/"
    assert client.get(url).status_code == 401
    stale = client.get(url, **auth())
    assert stale.status_code == 503
    validate_contract("OperationsStatus", stale.json())
    settings.RECURRING_CHARGES_ENABLED = False
    gateway = RecurringGateway()
    worker_tick(gateway)
    assert not sub.payments.exists()
    assert gateway.creates == 0
    status = client.get(url, **auth())
    assert status.status_code == 200
    validate_contract("OperationsStatus", status.json())
    assert ReconciliationRun.objects.get().success


@pytest.mark.django_db
def test_provider_transaction_cannot_confirm_two_distinct_checkouts(installation, payload, gateway):
    first, _ = create_payment(payload, "one", gateway)
    other, _ = create_payment({**payload, "reference": "other"}, "two", gateway)
    tx_id = gateway.pay(first)["id"]
    assert refresh_attempt(first.id, gateway).status == "paid"
    gateway.checkouts[other.sumup_id].update(status="PAID", transaction_id=tx_id)
    rejected = refresh_attempt(other.id, gateway)
    assert rejected.uncertain and not rejected.paid_at


@pytest.mark.django_db
def test_chargeback_is_signed_and_preserved_if_provider_omits_it_later(
    installation, payload, gateway
):
    attempt, _ = create_payment(payload, "one", gateway)
    transaction = gateway.pay(attempt)
    assert refresh_attempt(attempt.id, gateway).status == "paid"
    transaction["simple_status"] = "CHARGEBACK"
    disputed = refresh_attempt(attempt.id, gateway)
    assert disputed.status == "disputed"
    assert disputed.dispute_detected_at and disputed.refunded_amount == 0
    event = Notification.objects.first().payload
    assert event["payment"]["status"] == "disputed"
    validate_contract("PaymentUpdated", event)
    del transaction["simple_status"]
    assert refresh_attempt(attempt.id, gateway).status == "disputed"


@pytest.mark.django_db(transaction=True)
def test_postgres_two_schedulers_reserve_one_cycle(installation, plan, clock):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks.")
    sub, _ = create_subscription(plan)
    starting = Barrier(2)

    def evaluate():
        try:
            starting.wait(timeout=5)
            evaluate_subscription(sub.id, installation, clock[0])
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        workers = [pool.submit(evaluate) for _ in range(2)]
        for worker in workers:
            worker.result(timeout=15)
    assert sub.payments.count() == Attempt.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_postgres_two_dispatchers_submit_one_recurring_put(installation, plan, clock):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks.")
    gateway = RecurringGateway()
    sub, _ = create_subscription(plan)
    evaluate_subscription(sub.id, installation, clock[0])
    from payments.services import _create_checkout

    attempt = Attempt.objects.get()
    _create_checkout(attempt, gateway)
    starting = Barrier(2)

    def dispatch():
        try:
            starting.wait(timeout=5)
            return process_recurring(attempt.id, gateway)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatchers = [pool.submit(dispatch) for _ in range(2)]
        for dispatcher in dispatchers:
            dispatcher.result(timeout=15)
    assert gateway.processes == 1
    assert Attempt.objects.get().status == "paid"


@pytest.mark.django_db
def test_revoked_provider_mandate_stops_put_before_submission(installation, plan, clock):
    gateway = RecurringGateway()
    gateway.instrument_active = False
    sub, _ = create_subscription(plan)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(204))) as sender:
        worker_tick(gateway, sender)
        attempt = sub.payments.get().attempts.get()
        assert attempt.uncertain and attempt.process_submitted_at is None
        assert gateway.processes == 0
        gateway.instrument_active = True
        clock[0] += timedelta(seconds=61)
        worker_tick(gateway, sender)
    assert sub.payments.get().attempts.get().status == "paid"
    assert gateway.processes == 1
