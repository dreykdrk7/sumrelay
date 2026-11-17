# Saved cards and recurring charges

SumRelay provides payment primitives. Your application owns the subscription
schedule, pricing, taxes, consent, cancellation interface, and customer access.
Tokenization requires support on your SumUp merchant and customer authorization.

## Configuration

Set a separate Fernet key in `PAYMENT_TOKEN_ENCRYPTION_KEY` or its `_FILE` variant.
Generate it once and preserve it with encrypted configuration backups:

```sh
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Never replace an existing encryption key during an update. Existing tokens need
the original key. Card setup returns 503 if encryption is not configured. Card
setup and recurring charges require both `PAYMENT_CREATION_ENABLED=true` and
`RECURRING_CHARGES_ENABLED=true`. Read and cancellation operations remain available
when charge creation is paused.

## HTTP lifecycle

Use the same backend credential as the main API; never send it to the browser.
See the [OpenAPI contract](openapi.json) for complete request and response schemas.

1. `POST /api/v1/card-setups/`: submit a unique `reference` and an allowed
   `return_url`. SumRelay reserves a setup, creates a provider customer, and creates
   a `SETUP_RECURRING_PAYMENT` authorization checkout for 1 EUR. Responses contain
   an opaque card ID and public `setup_checkout_id`, never the payment token.
2. Mount the [SumUp card widget](https://developer.sumup.com/online-payments/checkouts/card-widget/)
   with that checkout and obtain consent to save the card. Card details go directly
   to SumUp. Your application handles customer interaction and authentication.
3. `GET /api/v1/card-setups/{id}/`: verify the provider's customer, merchant,
   amount, currency, transaction, and active recurring instrument/mandate. Only
   then is the card `ready` with `uncertain=false`. Tokens are encrypted at rest.
4. `POST /api/v1/recurring-payments/`: send the five ordinary payment fields plus
   `saved_card_id` and `Idempotency-Key`. SumRelay creates and processes a checkout
   for that customer, then verifies checkout and `RECURRING` transaction state.
5. `POST /api/v1/recurring-payments/cancel/`: send the same financial body used for
   creation, including `saved_card_id`, without needing an idempotency key. The
   cancellation is stored by order reference even if creation has not yet arrived.
   Known pending checkouts are deactivated and verified. Repeating cancellation is safe.
6. `POST /api/v1/payments/{id}/cancel/`: use this alternative when the payment ID
   is already known. This cancels an order's pending charge, not your application's
   entire subscription schedule or future order references.

Payments include `saved_card_id` and `cancel_requested`. Attempts include
`action_required` when SumUp returns a customer authentication step. Do not mark
these attempts paid or blindly submit the charge again; involve the customer.

## Crash recovery and retries

Setup references and submission markers are persisted before provider calls.
A lost setup response is recovered by looking up its reference, without creating
another setup automatically. Payment attempts are also reserved before sending.
`process_submitted_at` is committed before processing a token: after a failure,
SumRelay queries the checkout rather than automatically resubmitting the charge.

A `FAILED` checkout may still be payable. Recurring attempts must be deactivated
and checked before they can be treated as closed. An uncertain state blocks a new
attempt. Signed notifications and reconciliation also apply to recurring charges.

The setup authorization is distinct from the subscription price and does not
fulfill an order. Consult SumUp's [tokenization guide](https://developer.sumup.com/online-payments/guides/tokenization-with-payment-sdk/)
for provider behavior; verify authorization and refund state separately rather
than inferring a refund from the card being ready.
