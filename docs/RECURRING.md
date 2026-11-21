# Saved cards and recurring charges

SumRelay supports individual recurring charges and scheduled subscriptions.
Your application owns pricing, taxes, consent, cancellation UI and customer access.
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

## Scheduled subscriptions

Use the same backend credential for these routes. The application records payer
consent and owns the plan, price, cancellation UI and access. SumRelay stores
the agreed payment snapshot and runs the collection schedule.

| Method and path | Result |
| --- | --- |
| `POST /api/v1/subscriptions/` | Create the schedule. Repeating the same reference and data returns the existing schedule. Changing those data with the same reference returns 409. |
| `GET /api/v1/subscriptions/?reference=...` | Recover the subscription ID and current status by exact CRM reference. |
| `GET /api/v1/subscriptions/{id}/` | Read status, next collection date and latest payment ID. |
| `GET /api/v1/subscriptions/{id}/payments/` | Read up to 25 collections, newest first. Pass `next_before` as the next request's `before` query parameter. |
| `POST /api/v1/subscriptions/{id}/cancel/` | Record cancellation immediately. The worker reconciles an in-flight collection and confirms any checkout closure with SumUp. |

For creation send the five ordinary payment fields plus `saved_card_id`,
`starts_on` (YYYY-MM-DD, today or later in UTC) and one schedule: `interval_days: 30` for fixed 30-day terms, or
`interval_months` (1–12) for calendar-month agreements. New Python client
calls default to 30 days and send the interval explicitly.
`reference` identifies the subscription in your application. Keep it unique
and persist it before sending the request. After a timeout, repeat the original
request with the same terms to recover its ID. Creating a subscription records
a schedule. Its collections are processed by the worker.

Collections become eligible on their UTC date. Thirty-day cycles count calendar
dates from the first due date. A January 31, 2026 start renews on March 2 and
April 1. Monthly schedules keep their original convention: January 31 becomes
February 28 or 29 and returns to March 31. Migration 0006 preserves existing
schedules and payment history. Changing the interval on a repeated reference
returns a conflict. The payment ID and
`scheduled_for` trace each collection. Following a delayed successful collection,
the schedule advances to the first future date on the original cadence. The
worker does not issue a backlog of multiple collections at once.

An unresolved collection holds the schedule on the current date. A verified
closed collection without a full, undisputed payment leaves the schedule
`blocked` with a `blocked_reason`. Subscription cancellation stops new
collections. Poll the payment itself to determine whether an in-flight charge
succeeded or was closed. Cancellation does not initiate a refund.

When a pending attempt has `action_required=true`, contact the customer.
SumRelay detects the challenge and tracks the result through reconciliation.
To recover through a new customer-present payment, first verify closure of the
old checkout and the absence of a verified charge. Once the old schedule is
cancelled, a replacement schedule uses a fresh reference, consent and the
agreed future start date. An automatic resubmission is never made after an
ambiguous provider call.

Subscribe to signed `payment.updated` notifications. Each scheduled payment
includes `subscription_id` and `scheduled_for`. Deduplicate by event ID and
re-read the payment before applying the current state. Validate it against the
original subscription and authorize the cycle once in your own database,
using a unique constraint on `(subscription_id, scheduled_for)`. The payment
reference has the form `sumrelay-subscription:<UUID>:<YYYY-MM-DD>` and is
reserved by SumRelay.

A provider chargeback is exposed as `disputed`, with `dispute_detected_at` on
the attempt. Handle this as an exception even if the collection was originally
confirmed. Refund amounts retain their original meaning. A verified dispute
remains visible if a later provider response omits the disputed event.
