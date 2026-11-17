import hashlib
import hmac
import json
from functools import wraps

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import Attempt, Payment
from .serializers import serialize_attempt, serialize_payment
from .services import create_payment
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
    from django.db import transaction

    from .services import cancel_attempt

    with transaction.atomic():
        payment = Payment.objects.select_for_update().filter(pk=payment_id).first()
        if payment is None:
            raise PaymentError("not_found", "Pago no encontrado.", 404)
        if not payment.saved_card_id:
            raise PaymentError(
                "not_recurring", "La cancelación API se limita a cargos recurrentes.", 409
            )
        payment.cancel_requested_at = payment.cancel_requested_at or timezone.now()
        payment.save(update_fields=["cancel_requested_at"])
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
