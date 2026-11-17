import logging
import signal
import threading

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from payments.worker import worker_tick

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Ejecuta conciliación, comprobaciones pendientes y entrega de notificaciones."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stopped = threading.Event()
        if not options["once"]:
            signal.signal(signal.SIGTERM, lambda *_: stopped.set())
            signal.signal(signal.SIGINT, lambda *_: stopped.set())
        while not stopped.is_set():
            try:
                close_old_connections()
                worker_tick()
            except Exception as exc:
                # Provider responses may contain sensitive data; do not log exception bodies.
                logger.error("Ciclo del worker interrumpido (%s).", type(exc).__name__)
                if options["once"]:
                    raise SystemExit(1) from None
            if options["once"]:
                return
            stopped.wait(5)
