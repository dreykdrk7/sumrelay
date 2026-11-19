# Installation and operations

## Configure an installation

Use separate domains, databases, volumes, API credentials, admin accounts, and
SumUp merchants for sandbox and live environments. One installation serves one
application and one merchant. PostgreSQL is required for real operation; SQLite
is only used for fast unit tests.

```sh
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your own domain and merchant. Generate different random values
for `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`, `SAAS_API_KEYS`, and
`SAAS_WEBHOOK_SECRET` (at least 32 characters each). For example, run this separately
for each value and store its output privately:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

For saved cards, also set `PAYMENT_TOKEN_ENCRYPTION_KEY` to a Fernet key. A
dependency-free generator is:

```sh
python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

Keep this key for the lifetime of encrypted tokens. The configuration loader
supports `<NAME>_FILE` alternatives for secrets, including this key. Files must be
mounted inside the relevant containers. If using Docker secrets for PostgreSQL,
replace the database service's password variable with `POSTGRES_PASSWORD_FILE` too.

Set the domain consistently in `DJANGO_ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`,
`PUBLIC_BASE_URL`, and `APP_DOMAIN`. Point DNS to your server and make ports 80/443
available for the optional Caddy proxy. `TRUST_PROXY=true` is appropriate only
behind a controlled proxy that replaces `X-Forwarded-Proto`.

Keep `SUMUP_SANDBOX_ONLY=true` for a sandbox merchant. For a live merchant, set it
to `false` only after checking account access and completing sandbox validation.
Configure public HTTPS callbacks with `SUMUP_WEBHOOK_ENABLED=true`.

## Start and configure

Payment creation and recurring charges start disabled. Start the service, create
an individual admin account, and configure destinations:

```sh
docker compose --profile proxy up -d --build
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py check_sumup --connection-only
docker compose exec web python manage.py configure_installation \
  --notification-url https://app.example.com/webhooks/payments \
  --return-url https://app.example.com/payments/return \
  --currency EUR
```

`setup` validates deployment configuration, applies migrations, and collects
static files before web and worker startup. The database has no host port.
Caddy terminates TLS and serves admin static files. If you use your own proxy,
omit `--profile proxy`, configure static-file serving, and route to the web port
bound to localhost. A proxy in another Compose project needs an explicitly
shared network.

Open `https://YOUR_DOMAIN/admin/` and verify the integration. When ready, set
`PAYMENT_CREATION_ENABLED=true` in `.env`. Enable `RECURRING_CHARGES_ENABLED=true`
only if saved cards and recurring charges are required. Apply controls by
recreating the services:

```sh
docker compose up -d --force-recreate web worker
```

Disabled creation returns 503 before reserving or sending a new charge. Read,
reconciliation, and cancellation remain available; a paused worker does not
submit recurring charges. Pausing cannot undo a provider call already in flight.

## Credentials and destination changes

`SAAS_API_KEYS` accepts comma-separated backend keys for rotation. Add the new
key, recreate `web`, update your application, and remove the old key. Keys are
not separate tenants. Coordinate HMAC secret rotation with your receiver.

Do not change the merchant on an installation with payment history. Existing
orders preserve their merchant snapshot. `configure_installation` updates the
configured destinations, but queued events retain their original destination.
Register only destinations you control and exact return URLs.

## Monitor

```sh
docker compose ps
docker compose logs --tail=100 web worker setup
docker compose exec web python manage.py check --deploy --fail-level ERROR
```

`/health/` checks the database connection. The authenticated
`GET /api/v1/operations/status/` reports the last completed worker tick,
reconciliation timestamps, uncertain attempts, exhausted deliveries and
blocked subscriptions. A missing heartbeat or one older than 10 minutes
returns 503. Monitor the reconciliation timestamps separately for provider
connectivity and verification issues. The admin can request a
reconciliation, check or cancel attempts, and open another delivery retry window.
Audit records track admin actions but are not immutable against database administrators.

After an uncertain creation, the service looks up the attempt UUID with SumUp.
Zero or multiple matches keep the order blocked. Investigate rather than editing
financial state in the database. A provider `FAILED` status or local expiration
alone does not prove that a checkout can no longer be charged.

## Back up and update

Before updating, validate the new commit and migrations in sandbox. Put the app
in maintenance for new requests, preserve order/idempotency references, and stop
the payment writers for a coherent database backup:

```sh
docker compose stop web worker
mkdir -p backups
chmod 700 backups
# Choose a new dated filename each time; do not overwrite your last valid copy.
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > backups/sumrelay-YYYYMMDD-HHMM.dump
docker compose exec -T db pg_restore --list \
  < backups/sumrelay-YYYYMMDD-HHMM.dump
```

Check command exit status and actually rehearse restoration. Encrypt database
and configuration backups, store them off-host with access controls, and keep
encryption keys separately. Back up the secrets, proxy configuration, and source
commit too; static files can be regenerated. The supplied Compose stack does not
schedule or encrypt backups for you.

For an update, preserve the backup and configuration, then:

```sh
git pull --ff-only
docker compose build
docker compose run --rm setup
docker compose up -d --force-recreate web worker
```

Check health, admin, logs, and a full reconciliation before reopening requests.
Migrations must finish before starting processes with new code. A rollback must
be compatible with the current schema or restore a verified backup in isolation.

## Restore and verify

Restore into a separate Compose project with an empty database and no web,
worker, or proxy running. For a recovery rehearsal, also block outbound network
access: restored events may contain real destinations. Start only its database,
then restore using that project's configuration:

```sh
docker compose -p sumrelay-restore up -d db
# Wait for PostgreSQL health before restoring into the empty database.
docker compose -p sumrelay-restore exec -T db sh -c \
  'pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < backups/sumrelay-YYYYMMDD-HHMM.dump
```

Use the backed-up source commit and secrets. Compare table counts and known
orders, attempts, refund events, notifications, admins, and references. In a
rehearsal, do not start the worker or send old notifications.

For a real recovery, ensure the old installation is stopped, verify domain and
secrets, start the services, and request reconciliation. Notifications may repeat;
the application must deduplicate them. Reconcile any payments created after the
backup with SumUp and your app: SumRelay visits known checkouts and cannot
automatically recover orders missing from the restored database.

Record periodic restore drills and results. Choose backup frequency and retention
for your own data-loss tolerance. Separate Compose projects sharing one host or
administrator have a weaker boundary than separate hosts.
