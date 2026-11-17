# Integrating your backend

Your backend owns the order, price, currency, expected merchant, and fulfillment
record. SumRelay owns the checkout attempts and verified payment state. The
customer browser receives a checkout URL, never a server API key.

## Configure the connection

Use `configure_installation` or the admin to register your webhook destination
and exact allowed return URLs. Production destinations must use HTTPS.

```sh
docker compose exec web python manage.py configure_installation \
  --notification-url https://app.example.com/webhooks/payments \
  --return-url https://app.example.com/payments/return \
  --currency EUR
```

The [backend environment example](../clients/python/backend.env.example) lists
the settings to keep on your application server. Its API key must match one of
SumRelay's `SAAS_API_KEYS`; it is a different credential from `SUMUP_API_KEY`.

## Python example

The included [client](../clients/python/payment_client.py) depends on `httpx` and
Python 3.10+. You can copy the `clients` package into your backend, or use this
example from the repository root. It is not published as a PyPI package.

```python
import os
from decimal import Decimal

from clients.python.payment_client import ProcessorClient, checkout_url_for_redirect

with ProcessorClient(
    os.environ["PAYMENT_PROCESSOR_URL"],
    os.environ["PAYMENT_PROCESSOR_API_KEY"],
) as processor:
    payment = processor.create_payment(
        reference="ORDER-42",
        amount=Decimal("19.95"),
        currency="EUR",
        description="Example order",
        return_url="https://app.example.com/payments/return",
        idempotency_key="order-42-attempt-1",
    )
    checkout_url = checkout_url_for_redirect(payment)
    # Persist payment["id"] against your order.
    # Redirect the customer only when checkout_url is present.
```

Persist the order and its idempotency key before calling SumRelay. Reuse that key
and the same body after a lost response. An uncertain response requires querying
the existing payment; a new key must not become a workaround for uncertainty.
The client does not automatically retry POST requests.

## Verify and store webhook events

Read the exact raw request bytes before JSON parsing. The helper checks the
signature, timestamp tolerance, event identity, and basic event structure:

```python
from clients.python.payment_client import verify_webhook

event = verify_webhook(
    raw_body=request.body,
    headers=request.headers,
    secrets=webhook_secret,
)
# In a database transaction: insert event["id"] under a UNIQUE constraint.
# If it already exists, acknowledge the duplicate without repeating fulfillment.
# Return 2xx only after durable storage; process the stored event asynchronously.
```

`request` is your web framework's request and `webhook_secret` is the private
shared secret. Do not re-serialize JSON before signature verification. Events
may be repeated or arrive out of order. Query the current payment before making
a fulfillment decision based on a stale snapshot.

## Fulfill exactly once in your application

Query `get_payment(payment_id)` and validate against the original server-side
order using `confirm_for_order`:

```python
from clients.python.payment_client import confirm_for_order

confirmed = confirm_for_order(
    payment,
    payment_id=stored_payment_id,
    reference=stored_order_reference,
    amount=stored_order_amount,  # Decimal, not a browser-supplied value.
    currency=stored_order_currency,
    merchant_code=expected_merchant_code,
)
# Atomically record confirmed.payment_id / confirmed.transaction_id and fulfill
# the order once, guarded by a unique order-fulfillment constraint.
```

The helper rejects mismatches, refunds, uncertainty, multiple confirmed charges,
or still-open attempts. It does not write a fulfillment record for you. Decide
your policy for refunds and reconcile that policy with your own order history.
Browser return parameters never authorize delivery.

## Other languages and recurring charges

Any backend can use the [HTTP API](API.md) and [OpenAPI contract](openapi.json).
HMAC signatures use SHA-256 over `timestamp + "." + raw_body`; compare in constant
time and enforce a timestamp tolerance as well as persistent event deduplication.

See [recurring payments](RECURRING.md) for saved cards. Your application schedules
each renewal and owns consent, cancellation UI, pricing, and entitlement periods.
