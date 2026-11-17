#!/usr/bin/env python3
"""Create private, isolated local configuration without displaying any secrets."""

import base64
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
destination = root / ".env.sandbox"
if destination.exists():
    print("Se conserva .env.sandbox; no se han sustituido sus valores.")
else:
    values = {
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
        "DJANGO_DEBUG": "true",
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1",
        "CSRF_TRUSTED_ORIGINS": "http://localhost:8000,http://127.0.0.1:8000",
        "PUBLIC_BASE_URL": "http://localhost:8000",
        "TRUST_PROXY": "false",
        "APP_PORT": "8000",
        "POSTGRES_DB": "sumup_sandbox",
        "POSTGRES_USER": "sumup",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(48),
        "POSTGRES_HOST": "db",
        "POSTGRES_PORT": "5432",
        "PAYMENT_CREATION_ENABLED": "true",
        "RECURRING_CHARGES_ENABLED": "true",
        "SUMUP_SANDBOX_ONLY": "true",
        "SUMUP_WEBHOOK_ENABLED": "false",
        "PAYMENT_TOKEN_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "SUMUP_API_KEY": "",
        "SUMUP_MERCHANT_CODE": "",
        "SAAS_API_KEYS": secrets.token_urlsafe(48),
        "SAAS_WEBHOOK_SECRET": secrets.token_urlsafe(48),
        "SANDBOX_ADMIN_USERNAME": "admin",
        "SANDBOX_ADMIN_PASSWORD": secrets.token_urlsafe(24),
    }
    header = (
        "# Solo pruebas locales. Archivo privado; no subir a Git.\n"
        "# Rellena SUMUP_API_KEY y SUMUP_MERCHANT_CODE desde el comercio SANDBOX.\n"
        "# La contraseña del panel local es SANDBOX_ADMIN_PASSWORD.\n"
    )
    with open(destination, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
        f.write(header + "".join(f"{key}={value}\n" for key, value in values.items()))
    print("Creado .env.sandbox con permisos 600 y secretos locales independientes.")
