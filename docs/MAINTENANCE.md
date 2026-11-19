# Maintenance after v1.1.0

Version [v1.1.0](https://github.com/dreykdrk7/sumrelay/releases/tag/v1.1.0)
is the validated baseline, commit `e2880f3b478cff14c68a13f5c4c5523dded09119`.
Its tag and release remain fixed. Later operational documentation does not
change the released runtime or activate payment creation.

## Finite scope

Active feature development is closed for this version. Maintenance covers
reproducible payment-correctness defects, security updates and compatibility
changes required by SumUp or supported dependencies. New product features
need a concrete integration need and a separate proposal. There is no ongoing
commitment to expand the roadmap or provide paid support.

## Operator responsibilities

- Keep encrypted database/configuration backups off the application host.
  Preserve the payment-token Fernet key in the recovery package and keep the
  independent archive-decryption key separately. Choose a retention period.
- Rehearse restoration in an isolated database with no application worker or
  outbound network. Compare table counts and row fingerprints before accepting
  a backup. Reconcile provider state before enabling charges after a recovery.
- Monitor the worker, reconciliation freshness, uncertain payments, exhausted
  or overdue deliveries, storage and backup freshness. Send state changes and
  recovery notices to an operator, with retryable delivery and deduplication.
- Review security/dependency notices. Dependabot proposes weekly updates;
  changes still need review and the relevant tests before deployment.

The supplied application Compose file does not install host backup/alert
timers. Configure these for each installation; the authenticated operational
API is described in [operations](OPERATIONS.md).

## Validation and bounded fixtures

The release passed 132 automated tests with Python 3.12 and PostgreSQL 17,
including concurrency, and an upgrade rehearsal from migration 0003 to 0005.
The Eventora acceptance used real SumUp sandbox with one merchant, saved test
card and test company: tokenization/3DS, a 19.95 EUR collection, matching
provider/relay/admin records, signed-event replay without a duplicate cycle,
full refund and cancellation with no next collection date.

The only additional operational fixtures needed for the reference staging
closure are an offsite restore drill and an incident/recovery alert drill.
Corrupt archives must fail verification; unchanged incidents must not spam
the operator. There is no new payment feature or live-charge experiment in
this maintenance phase.

## License

The project remains under [MIT](../LICENSE), including commercial use under
its notice and license conditions. Keep that notice when redistributing the
code. Dependencies retain their own licenses. SumRelay is independent of SumUp.
