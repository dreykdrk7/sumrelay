import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Crea el administrador inicial de la instalación local sandbox sin mostrar su contraseña."
    )

    def handle(self, *args, **options):
        if not settings.DEBUG or not settings.SUMUP_SANDBOX_ONLY:
            raise CommandError("Esta orden solo está disponible para desarrollo local sandbox.")
        username = os.getenv("SANDBOX_ADMIN_USERNAME", "")
        password = os.getenv("SANDBOX_ADMIN_PASSWORD", "")
        if not username or len(password) < 16:
            raise CommandError("Faltan las credenciales locales SANDBOX_ADMIN_USERNAME/PASSWORD.")
        user_model = get_user_model()
        if user_model.objects.filter(username=username).exists():
            self.stdout.write("Se conserva el administrador existente y su contraseña.")
            return
        user_model.objects.create_superuser(username=username, email="", password=password)
        self.stdout.write(
            self.style.SUCCESS("Administrador local creado. Contraseña en .env.sandbox.")
        )
