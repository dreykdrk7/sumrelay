import json
from pathlib import Path
from unittest.mock import patch

import pytest
from django.conf import settings
from jsonschema import Draft202012Validator

from payments.gateway import ProviderError
from payments.models import Notification
from payments.serializers import serialize_payment
from payments.services import create_payment, refresh_attempt

CONTRACT = json.loads((Path(__file__).resolve().parents[1] / "docs" / "openapi.json").read_text())


def validate_contract(name, value):
    schema = {"$ref": f"#/components/schemas/{name}", "components": CONTRACT["components"]}
    Draft202012Validator(schema).validate(value)


def auth():
    return {"HTTP_AUTHORIZATION": f"Bearer {settings.SAAS_API_KEYS[0]}"}


def test_component_schemas_are_valid_json_schema():
    for schema in CONTRACT["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)


@pytest.mark.django_db
def test_create_and_query_responses_match_contract(client, installation, payload, gateway):
    validate_contract("CreatePayment", payload)
    with patch("payments.services.SumUpGateway") as factory:
        factory.return_value.__enter__.return_value = gateway
        for expected_status in [201, 200]:
            response = client.post(
                "/api/v1/payments/",
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY="key",
                **auth(),
            )
            assert response.status_code == expected_status
            validate_contract("CreatePaymentResult", response.json())
    validate_contract("Payment", client.get(response["Location"], **auth()).json())
    validate_contract(
        "Payment", client.get("/api/v1/payments/?reference=ORDER-42", **auth()).json()
    )


@pytest.mark.django_db
def test_uncertain_creation_response_matches_contract(client, installation, payload, gateway):
    gateway.create_error = ProviderError("Respuesta perdida")
    with patch("payments.services.SumUpGateway") as factory:
        factory.return_value.__enter__.return_value = gateway
        response = client.post(
            "/api/v1/payments/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="key",
            **auth(),
        )
    assert response.status_code == 202 and response.json()["uncertain"]
    validate_contract("CreatePaymentResult", response.json())


@pytest.mark.django_db
def test_payment_and_partial_refund_notifications_match_contract(installation, payload, gateway):
    attempt, _ = create_payment(payload, "key", gateway)
    detail = gateway.pay(attempt)
    refresh_attempt(attempt.pk, gateway)
    detail["status"] = "REFUNDED"
    detail["events"] = [{"id": 1, "type": "REFUND", "status": "REFUNDED", "amount": 5}]
    refresh_attempt(attempt.pk, gateway)
    payment = serialize_payment(attempt.payment)
    assert payment["status"] == "partially_refunded"
    validate_contract("Payment", payment)
    for notification in Notification.objects.all():
        validate_contract("PaymentUpdated", notification.payload)


@pytest.mark.django_db
def test_error_and_schema_endpoint_match_contract(client):
    response = client.get("/api/v1/payments/?reference=unknown")
    assert response.status_code == 401 and response["Cache-Control"] == "no-store"
    validate_contract("Error", response.json())
    response = client.get("/api/v1/openapi.json", **auth())
    assert response.status_code == 200 and response.json() == CONTRACT
    response = client.get("/api/v1/payments/?reference=unknown", **auth())
    assert response.status_code == 404
    validate_contract("Error", response.json())
