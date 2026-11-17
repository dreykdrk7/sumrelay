from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from payments.gateway import ProviderError, SumUpGateway


class Command(BaseCommand):
    help = "Comprueba credenciales y comercio mediante consultas de lectura. No crea pagos."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--require-sandbox", action="store_true")
        mode.add_argument(
            "--connection-only",
            action="store_true",
            help="Diagnóstico de lectura: no exige sandbox ni cambia el bloqueo de cobros.",
        )

    def handle(self, *args, **options):
        if not settings.SUMUP_API_KEY or not settings.SUMUP_MERCHANT_CODE:
            raise CommandError(
                "Rellena SUMUP_API_KEY y SUMUP_MERCHANT_CODE en .env.sandbox y reinicia."
            )
        try:
            with SumUpGateway() as gateway:
                account = gateway.account(settings.SUMUP_MERCHANT_CODE)
                if account["merchant_code"] != settings.SUMUP_MERCHANT_CODE:
                    raise CommandError("El comercio recibido no coincide con el configurado.")
                gateway.find(f"connection-check-{uuid4().hex}")
        except ProviderError:
            raise CommandError(
                "SumUp no ha permitido comprobar el comercio. "
                "Revisa la clave, el código y la conexión."
            ) from None
        self.stdout.write(self.style.SUCCESS("Credenciales aceptadas por SumUp."))
        self.stdout.write("Comercio coincidente y acceso de lectura a checkouts: correctos.")
        sandbox_label = {True: "sí", False: "no (comercio real)", None: "no informado por SumUp"}[
            account["sandbox"]
        ]
        self.stdout.write(
            f"Comercio: {account['merchant_code']}; moneda: {account['currency']}; "
            f"sandbox: {sandbox_label}"
        )
        strict = options["require_sandbox"] or settings.SUMUP_SANDBOX_ONLY
        if not options["connection_only"] and strict and account["sandbox"] is not True:
            raise CommandError(
                "El comercio no está confirmado como sandbox. "
                "La conexión es correcta, pero la creación de cobros permanece bloqueada."
            )
        if options["connection_only"]:
            self.stdout.write("Diagnóstico de solo lectura; no se han creado ni procesado pagos.")
            if settings.SUMUP_SANDBOX_ONLY:
                self.stdout.write(
                    "Se mantiene SUMUP_SANDBOX_ONLY=true; el bloqueo no se ha cambiado."
                )
        if urlsplit(settings.PUBLIC_BASE_URL).hostname in {"localhost", "127.0.0.1"}:
            self.stdout.write(
                "Retorno local configurado: los callbacks desde SumUp aún no son accesibles."
            )
