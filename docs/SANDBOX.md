# Local sandbox

The sandbox helper uses a separate Compose project named `sumrelay-sandbox`,
private `.env.sandbox` configuration, a PostgreSQL volume, and a local webhook
receiver. It creates no live merchant credentials and never copies another
installation's configuration.

```sh
scripts/sandbox.sh init
# Edit .env.sandbox: sandbox SUMUP_API_KEY and SUMUP_MERCHANT_CODE.
scripts/sandbox.sh up
scripts/sandbox.sh check-credentials
```

`init` generates independent Django, database, API, HMAC, token encryption, and
admin secrets. Existing files and admin passwords are preserved. The sandbox
enables payment endpoints but forces `SUMUP_SANDBOX_ONLY=true`: the gateway must
receive explicit sandbox confirmation from SumUp before creating a checkout.

`check-credentials` only reads account information and checks reference lookup;
it creates no payments. If SumUp omits the optional sandbox field, verification
fails closed. `check-connection` can establish read access without claiming that
the account is sandbox. It does not change the payment guard.

## Local URLs

| Purpose | URL |
| --- | --- |
| Admin | `http://localhost:8000/admin/` |
| Webhook receiver and event viewer | `http://localhost:8001/` |
| Browser return | `http://localhost:8001/return/` |
| Internal notification destination | `http://receiver.local:8001/notifications/` |

Only the web and receiver ports are bound to localhost. PostgreSQL has no host
port. The receiver verifies HMAC signatures, stores events, and deduplicates their
IDs. It is an example development service; it does not activate real products.

`SUMUP_WEBHOOK_ENABLED=false` omits the unreachable localhost callback. The worker
queries SumUp to discover results. Testing callbacks requires a public HTTPS
installation and explicitly enabling them.

## What to validate for your integration

Use SumUp's [official test cards](https://developer.sumup.com/online-payments/testing/)
and sandbox accounts. Validate success, rejection, customer authentication,
pending/uncertain results, cancellation, expiry, refunds, lost notifications,
duplicate event delivery, worker restarts, and backup recovery. For saved cards,
validate explicit consent, token setup, renewal, and cancellation separately.

Do not treat unit tests as proof that a merchant supports a feature. Provider
capabilities, payment methods, and authentication requirements must be checked
with your own sandbox configuration.

```sh
scripts/sandbox.sh status
scripts/sandbox.sh logs
scripts/sandbox.sh down
```

`down` preserves volumes. Do not use the localhost receiver or sandbox Compose
override for a public production service.
