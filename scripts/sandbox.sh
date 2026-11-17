#!/bin/sh
set -eu
project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
env_file="$project_dir/.env.sandbox"
compose() {
    APP_ENV_FILE="$env_file" docker compose --env-file "$env_file" -p sumrelay-sandbox \
        -f compose.yaml -f compose.sandbox.yaml "$@"
}
case "${1:-status}" in
    init)
        python3 scripts/init_sandbox.py
        ;;
    up)
        python3 scripts/init_sandbox.py
        compose up -d --build web worker receiver
        compose exec -T web python manage.py setup_sandbox_admin
        compose exec -T web python manage.py shell -c \
            "from payments.models import IntegrationConfig; from django.core.management import call_command; IntegrationConfig.objects.exists() or call_command('configure_installation', notification_url='http://receiver.local:8001/notifications/', return_url=['http://localhost:8001/return/'], currency=['EUR'])"
        printf '%s\n' 'Panel: http://localhost:8000/admin/' \
            'Receptor local de notificaciones: http://localhost:8001/' \
            'Usuario y contraseña locales: SANDBOX_ADMIN_USERNAME/PASSWORD en .env.sandbox.'
        ;;
    check-credentials)
        compose exec -T web python manage.py check_sumup --require-sandbox
        ;;
    check-connection)
        compose exec -T web python manage.py check_sumup --connection-only
        ;;
    status)
        compose ps
        ;;
    logs)
        compose logs --tail=50 web worker setup receiver
        ;;
    down)
        compose down
        ;;
    *)
        printf '%s\n' \
            'Uso: scripts/sandbox.sh init|up|check-connection|check-credentials|status|logs|down' >&2
        exit 2
        ;;
esac
