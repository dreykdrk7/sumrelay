import hashlib
import hmac
import json
from datetime import date, timedelta
from functools import wraps

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import Attempt, Notification, Payment, ReconciliationRun, Subscription, WorkerHeartbeat
from .serializers import iso, serialize_attempt, serialize_payment
from .services import create_payment
from .subscriptions import cancel_subscription, create_subscription, serialize_subscription
from .validation import PaymentError


def api_view(function):
    @wraps(function)
    def wrapper(request, *args, **kwargs):
        value = request.headers.get("Authorization", "")
        token = value.removeprefix("Bearer ") if value.startswith("Bearer ") else ""
        digest = hashlib.sha256(token.encode()).digest()
        valid = False
        for candidate in settings.SAAS_API_KEYS:
            valid |= hmac.compare_digest(digest, hashlib.sha256(candidate.encode()).digest())
        if not token or not valid:
            response = JsonResponse(
                {"error": {"code": "unauthorized", "message": "Credencial inválida."}}, status=401
            )
            response["WWW-Authenticate"] = "Bearer"
            response["Cache-Control"] = "no-store"
            return response
        try:
            response = function(request, *args, **kwargs)
        except PaymentError as exc:
            response = JsonResponse(
                {"error": {"code": exc.code, "message": exc.message}}, status=exc.status
            )
        response["Cache-Control"] = "no-store"
        return response

    return wrapper


def read_json(request):
    if request.content_type != "application/json":
        raise PaymentError("unsupported_media_type", "Usa Content-Type: application/json.", 415)
    try:
        return json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError, RequestDataTooBig):
        raise PaymentError("invalid_json", "JSON inválido o demasiado grande.") from None


@csrf_exempt
@require_http_methods(["POST", "GET"])
@api_view
def payments(request):
    if request.method == "POST":
        attempt, created = create_payment(
            read_json(request), request.headers.get("Idempotency-Key")
        )
        status = 202 if attempt.uncertain else 201 if created else 200
        response = JsonResponse(
            {**serialize_payment(attempt.payment), "requested_attempt": serialize_attempt(attempt)},
            status=status,
        )
        response["Location"] = f"/api/v1/payments/{attempt.payment_id}/"
        return response
    reference = request.GET.get("reference")
    if not reference:
        raise PaymentError("reference_required", "Indica una referencia exacta.")
    payment = Payment.objects.filter(reference=reference).first()
    if payment is None:
        raise PaymentError("not_found", "Pago no encontrado.", 404)
    return JsonResponse(serialize_payment(payment))


@require_GET
@api_view
def payment_detail(request, payment_id):
    payment = Payment.objects.filter(pk=payment_id).first()
    if payment is None:
        raise PaymentError("not_found", "Pago no encontrado.", 404)
    return JsonResponse(serialize_payment(payment))


@csrf_exempt
@require_POST
@api_view
def card_setups(request):
    from .cards import serialize_card, start_card

    card = start_card(read_json(request))
    return JsonResponse(serialize_card(card), status=202 if card.uncertain else 200)


@require_GET
@api_view
def card_snapshot(request, card_id):
    from .cards import serialize_card
    from .models import SavedCard

    card = SavedCard.objects.filter(pk=card_id).first()
    if card is None:
        raise PaymentError("not_found", "Alta no encontrada.", 404)
    return JsonResponse(serialize_card(card))


@require_GET
@api_view
def card_detail(request, card_id):
    from .cards import refresh_card, serialize_card
    from .models import SavedCard

    if not SavedCard.objects.filter(pk=card_id).exists():
        raise PaymentError("not_found", "Alta no encontrada.", 404)
    return JsonResponse(serialize_card(refresh_card(card_id)))


@csrf_exempt
@require_POST
@api_view
def recurring_payments(request):
    from .cards import process_recurring

    data = read_json(request)
    if not isinstance(data, dict) or "saved_card_id" not in data:
        raise PaymentError("invalid_request", "Indica la tarjeta guardada.")
    card_id = data.pop("saved_card_id")
    from uuid import UUID

    try:
        if not isinstance(card_id, str):
            raise ValueError
        card_id = UUID(card_id)
    except (ValueError, TypeError):
        raise PaymentError("invalid_card_id", "Identificador de tarjeta inválido.") from None
    attempt, _ = create_payment(data, request.headers.get("Idempotency-Key"), saved_card_id=card_id)
    attempt = process_recurring(attempt.id)
    return JsonResponse(
        serialize_payment(attempt.payment), status=202 if attempt.uncertain else 200
    )


@require_GET
@api_view
def api_schema(request):
    schema_file = settings.BASE_DIR / "docs" / "openapi.json"
    return JsonResponse(json.loads(schema_file.read_text()))


@csrf_exempt
@require_POST
@api_view
def payment_cancel(request, payment_id):
    from .services import cancel_attempt, request_payment_cancellation

    payment = Payment.objects.filter(pk=payment_id).first()
    if payment is None:
        raise PaymentError("not_found", "Pago no encontrado.", 404)
    if not payment.saved_card_id:
        raise PaymentError(
            "not_recurring", "La cancelación API se limita a cargos recurrentes.", 409
        )
    payment = request_payment_cancellation(payment)
    for attempt in payment.attempts.filter(closed_confirmed=False):
        cancel_attempt(attempt.id, None)
    return JsonResponse(serialize_payment(payment))


@csrf_exempt
@require_POST
@api_view
def recurring_cancel(request):
    from .cards import cancel_recurring_order

    return JsonResponse(serialize_payment(cancel_recurring_order(read_json(request))))


@csrf_exempt
@require_http_methods(["POST", "GET"])
@api_view
def subscriptions(request):
    if request.method == "GET":
        reference = request.GET.get("reference")
        if not reference:
            raise PaymentError("reference_required", "Indica una referencia exacta.")
        subscription = Subscription.objects.filter(reference=reference).first()
        if subscription is None:
            raise PaymentError("not_found", "Suscripción no encontrada.", 404)
        return JsonResponse(serialize_subscription(subscription))
    subscription, created = create_subscription(read_json(request))
    response = JsonResponse(serialize_subscription(subscription), status=201 if created else 200)
    response["Location"] = f"/api/v1/subscriptions/{subscription.id}/"
    return response


def get_subscription(subscription_id):
    subscription = Subscription.objects.filter(pk=subscription_id).first()
    if subscription is None:
        raise PaymentError("not_found", "Suscripción no encontrada.", 404)
    return subscription


@require_GET
@api_view
def subscription_detail(request, subscription_id):
    return JsonResponse(serialize_subscription(get_subscription(subscription_id)))


@require_GET
@api_view
def subscription_payments(request, subscription_id):
    subscription = get_subscription(subscription_id)
    payments = subscription.payments.order_by("-scheduled_for")
    if "before" in request.GET:
        try:
            before = date.fromisoformat(request.GET["before"])
            if before.isoformat() != request.GET["before"]:
                raise ValueError
        except ValueError:
            raise PaymentError("invalid_date", "Usa una fecha AAAA-MM-DD.") from None
        payments = payments.filter(scheduled_for__lt=before)
    page = list(payments[:26])
    return JsonResponse(
        {
            "id": str(subscription.id),
            "payments": [serialize_payment(payment) for payment in page[:25]],
            "next_before": iso(page[24].scheduled_for) if len(page) > 25 else None,
        }
    )


@csrf_exempt
@require_POST
@api_view
def subscription_cancel(request, subscription_id):
    return JsonResponse(serialize_subscription(cancel_subscription(subscription_id)))


@require_GET
@api_view
def operational_status(request):
    heartbeat = WorkerHeartbeat.objects.filter(pk=1).first()
    checked_at = heartbeat.checked_at if heartbeat else None
    healthy = checked_at is not None and checked_at > timezone.now() - timedelta(minutes=10)
    latest = ReconciliationRun.objects.first()
    last_success = ReconciliationRun.objects.filter(success=True).first()
    return JsonResponse(
        {
            "status": "ok" if healthy else "worker_stale",
            "worker_checked_at": iso(checked_at),
            "last_reconciliation_started_at": iso(latest.started_at if latest else None),
            "last_reconciliation_finished_at": iso(latest.finished_at if latest else None),
            "last_successful_reconciliation_at": iso(
                last_success.finished_at if last_success else None
            ),
            "uncertain_attempts": Attempt.objects.filter(uncertain=True).count(),
            "exhausted_notifications": Notification.objects.filter(exhausted=True).count(),
            "blocked_subscriptions": Subscription.objects.filter(
                status=Subscription.Status.BLOCKED
            ).count(),
        },
        status=200 if healthy else 503,
    )


@csrf_exempt
@require_POST
def sumup_webhook(request):
    try:
        data = read_json(request)
    except PaymentError:
        return HttpResponse(status=400)
    if not isinstance(data, dict):
        return HttpResponse(status=400)
    if data.get("event_type") != "CHECKOUT_STATUS_CHANGED":
        return HttpResponse(status=204)
    checkout_id = data.get("id")
    if not isinstance(checkout_id, str) or len(checkout_id) > 128:
        return HttpResponse(status=400)
    # This unauthenticated hint cannot change financial state; worker always queries SumUp.
    Attempt.objects.filter(sumup_id=checkout_id, check_requested_at__isnull=True).update(
        check_requested_at=timezone.now()
    )
    return HttpResponse(status=204)


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
