from decimal import Decimal
from urllib.parse import urlsplit

from django.conf import settings
from sumup import APIError, Sumup


class ProviderError(Exception):
    """Safe error: never propagate SDK response bodies or headers to logs or API."""


class ProviderRejected(ProviderError):
    """A documented HTTP rejection proves that this create request was not accepted."""


class SumUpGateway:
    def __init__(self):
        self.client = Sumup(api_key=settings.SUMUP_API_KEY, timeout=settings.SUMUP_TIMEOUT)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client._client.close()

    def _call(self, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except APIError as exc:
            if exc.status in {400, 401, 403}:
                raise ProviderRejected("SumUp ha rechazado la solicitud.") from None
            raise ProviderError("No se pudo verificar la operación con SumUp.") from None
        except Exception:
            raise ProviderError("No se pudo verificar la operación con SumUp.") from None

    def create(self, attempt):
        if settings.SUMUP_SANDBOX_ONLY:
            account = self.account(attempt.payment.merchant_code)
            if account["sandbox"] is not True:
                raise ProviderRejected("El modo sandbox exige un comercio sandbox verificado.")
        # The official SDK uses float at its boundary. The accepted range round-trips cents.
        value = float(attempt.payment.amount)
        if Decimal(str(value)) != attempt.payment.amount:
            raise ProviderError("Importe no representable por el SDK.")
        card = getattr(attempt.payment, "saved_card", None)
        result = self._call(
            self.client.checkouts.create,
            checkout_reference=str(attempt.id),
            amount=value,
            currency=attempt.payment.currency,
            merchant_code=attempt.payment.merchant_code,
            description=attempt.description,
            redirect_url=attempt.return_url,
            **(
                {"return_url": f"{settings.PUBLIC_BASE_URL}/webhooks/sumup/"}
                if settings.SUMUP_WEBHOOK_ENABLED
                else {}
            ),
            valid_until=attempt.expires_at,
            **(
                {"customer_id": card.customer_id, "purpose": "CHECKOUT"}
                if card
                else {"hosted_checkout": {"enabled": True}}
            ),
        )
        return result.model_dump(mode="json")

    def ensure_customer(self, card):
        try:
            result = self.client.customers.get(card.customer_id)
        except APIError as exc:
            if exc.status != 404:
                raise ProviderError("No se pudo verificar el cliente de SumUp.") from None
            result = self._call(self.client.customers.create, customer_id=card.customer_id)
        except Exception:
            raise ProviderError("No se pudo verificar el cliente de SumUp.") from None
        if result.customer_id != card.customer_id:
            raise ProviderError("Cliente de SumUp incoherente.")

    def create_card_setup(self, card):
        return self._call(
            self.client.checkouts.create,
            checkout_reference=str(card.id),
            amount=1.0,
            currency="EUR",
            merchant_code=card.merchant_code,
            customer_id=card.customer_id,
            purpose="SETUP_RECURRING_PAYMENT",
            description="Alta de tarjeta para pagos periódicos",
            redirect_url=card.return_url,
            valid_until=card.expires_at,
        ).model_dump(mode="json")

    def instruments(self, customer_id):
        return [
            item.model_dump(mode="json")
            for item in self._call(self.client.customers.list_payment_instruments, customer_id)
        ]

    def process_card(self, attempt, token):
        card = attempt.payment.saved_card
        account = self.account(attempt.payment.merchant_code)
        if account["merchant_code"] != attempt.payment.merchant_code:
            raise ProviderRejected("El comercio de SumUp no coincide con el pedido.")
        if settings.SUMUP_SANDBOX_ONLY and account["sandbox"] is not True:
            raise ProviderRejected("Solo se permite el comercio sandbox verificado.")
        return self._call(
            self.client.checkouts.process,
            attempt.sumup_id,
            payment_type="card",
            token=token,
            customer_id=card.customer_id,
        ).model_dump(mode="json")

    def get(self, checkout_id):
        return self._call(self.client.checkouts.get, checkout_id).model_dump(mode="json")

    def find(self, reference):
        result = self._call(self.client.checkouts.list, checkout_reference=reference)
        return [item.model_dump(mode="json") for item in result]

    def deactivate(self, checkout_id):
        return self._call(self.client.checkouts.deactivate, checkout_id).model_dump(mode="json")

    def transaction(self, merchant_code, transaction_id):
        return self._call(
            self.client.transactions.get,
            merchant_code,
            id=transaction_id,
        ).model_dump(mode="json")

    def account(self, merchant_code):
        merchant = self._call(self.client.merchants.get, merchant_code)
        return {
            "merchant_code": merchant.merchant_code,
            "currency": merchant.default_currency,
            "sandbox": merchant.sandbox,
        }


def safe_checkout_url(value):
    try:
        parsed = urlsplit(value or "")
        if (
            parsed.scheme == "https"
            and parsed.hostname == "checkout.sumup.com"
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
        ):
            return value
    except ValueError:
        pass
    return ""
