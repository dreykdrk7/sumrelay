from django.core.management.base import BaseCommand, CommandError

from payments.models import AuditLog, IntegrationConfig


class Command(BaseCommand):
    help = "Configura los destinos de integración (sin credenciales). También editable en /admin/."

    def add_arguments(self, parser):
        parser.add_argument("--notification-url", required=True)
        parser.add_argument("--return-url", action="append", required=True)
        parser.add_argument("--currency", action="append", default=None)

    def handle(self, *args, **options):
        config = IntegrationConfig.objects.filter(pk=1).first() or IntegrationConfig(pk=1)
        config.notification_url = options["notification_url"]
        config.allowed_return_urls = options["return_url"]
        config.currencies = options["currency"] or ["EUR"]
        try:
            config.full_clean()
        except Exception:
            raise CommandError("Configuración inválida: revisa las URLs y las monedas.") from None
        config.save()
        AuditLog.objects.create(
            actor_name="configure_installation (CLI)",
            action="config_changed",
            target="1",
            outcome="Configuración actualizada desde la consola del servidor",
        )
        self.stdout.write(self.style.SUCCESS("Instalación configurada."))
