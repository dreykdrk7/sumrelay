# Community roadmap

This is a direction for contributions, not a delivery schedule.

## Available today

- Hosted checkouts, persistent idempotency, and provider-verified states.
- Signed notifications, retries, reconciliation, and refund detection.
- Saved-card setup and recurring charge primitives.
- Docker deployment, local sandbox, admin tools, and a Python backend client.

## Useful next steps

- Full English localization of the admin panel, errors, and OpenAPI descriptions.
- More backend examples and clients for TypeScript, PHP, and other ecosystems.
- A worker heartbeat, structured metrics, and clearer queue visibility.
- Reproducible sandbox integration checks across supported merchant capabilities.
- Documented release/versioning and upgrade compatibility policies.
- A deployment-independent encrypted backup and restore helper.
- Improvements to customer authentication flows for recurring payments.

Bring proposals to [Issues](https://github.com/dreykdrk7/sumrelay/issues).
Payment correctness, understandable integration, and operational recovery take
priority over adding payment methods or expanding the service boundary.

Multi-merchant routing, subscription scheduling, invoicing, and starting refunds
are outside the current scope. Proposals should explain the API and recovery
implications before introducing them.
