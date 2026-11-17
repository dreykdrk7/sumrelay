# Security policy

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** button in the
[Security tab](https://github.com/dreykdrk7/sumrelay/security) to report privately.
Include the affected commit, impact, and a minimal reproduction with synthetic data.
Do not post exploit details, keys, tokens, or customer data in a public issue.

This is a volunteer community project. There is no response-time guarantee or
paid security support. Security fixes target the latest `main` branch; there are
currently no separately maintained release branches.

## Deployment boundaries

SumRelay's API is for trusted backends. Never expose `SAAS_API_KEYS`, the SumUp key,
the webhook secret, or the token encryption key to browser code. Different API
keys are rotation credentials for the same installation, not separate tenants.

Use HTTPS, individual admin accounts, restricted configuration files, and isolated
sandbox and live environments. The included HTTP webhook receiver is a local
development tool. Production receivers must verify signatures over the exact raw
body, enforce timestamp tolerance, and persistently deduplicate events.

Keep the Fernet key with encrypted, access-controlled backups. Losing it prevents
decrypting saved payment tokens. Compromise of a backup together with its secrets
can expose those tokens. A database administrator can change local audit records.

SumRelay never asks your backend to handle card numbers or CVV. Hosted checkout
and card-widget details go directly to SumUp. Do not add sensitive provider
responses, request authorization headers, or payment tokens to logs.

See [operations](docs/OPERATIONS.md) for backup and recovery behavior and the
[README](README.md#scope-and-current-limits) for current limits.
