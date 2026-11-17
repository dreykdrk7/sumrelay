# Contributing to SumRelay

Welcome! Reports, documentation, translations, tests, and integrations are useful
contributions. English and Spanish issues and pull requests are welcome.

For vulnerabilities, use the private process in [SECURITY.md](SECURITY.md).
For other bugs, include the commit, Python/Docker versions, synthetic steps to
reproduce, expected behavior, and actual behavior. Remove credentials, merchant
data, payment tokens, and personal information before sharing logs.

## Set up development

Fork the repository and create a branch for your change. Python 3.12 is the
version tested in CI and used by the Docker image.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python manage.py check --settings=config.test_settings
.venv/bin/python manage.py makemigrations --check --dry-run --settings=config.test_settings
```

The default test database is SQLite. PostgreSQL is required to test row locking
and concurrency. To run the full suite against a disposable database:

```sh
docker run -d --name sumrelay-test-db \
  -e POSTGRES_PASSWORD=local-test-only \
  -e POSTGRES_USER=sumup -e POSTGRES_DB=sumup \
  -p 127.0.0.1:55439:5432 postgres:17-alpine
# Wait until: docker exec sumrelay-test-db pg_isready -U sumup
TEST_POSTGRES=1 POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55439 \
  POSTGRES_PASSWORD=local-test-only .venv/bin/pytest -q
docker rm -f sumrelay-test-db
docker build -t sumrelay:dev .
```

Tests use fakes and HTTP mocks, not a real merchant. Do not add tests that create
live charges. Keep provider experiments in an isolated sandbox with explicit
credentials. Never commit `.env` files, dumps, or real checkout evidence.

## Changes that need particular care

- Preserve the order snapshot and idempotency behavior across retries and crashes.
- Preserve uncertain states until verified with the provider.
- Keep webhook verification, durable deduplication, and redaction of secrets intact.
- Update `docs/openapi.json` and documentation when the API changes.
- Include Django migrations for model changes and explain compatibility for existing installations.
- Test new payment behavior at failure boundaries, including lost responses and concurrency.

Keep pull requests focused. Explain the problem, the resulting behavior, and the
checks you ran. New integrations should use generic examples and synthetic data.
Discuss large API or architectural changes in an issue before implementing them.

Contributions are provided under this project's [MIT license](LICENSE).
Be respectful, address the technical issue, and help other contributors reproduce
and understand it. Harassment and publication of private information are unwelcome.
