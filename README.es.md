# SumRelay

**Pagos de SumUp verificados para tu aplicación. Autoalojado, abierto y pensado para developers.**

[English](README.md) · [API](docs/API.md) · [Contribuir](CONTRIBUTING.md)

SumRelay conecta tu backend con SumUp: crea checkouts alojados, verifica el cobro
con el proveedor y envía notificaciones firmadas a tu aplicación. Tu aplicación
mantiene el catálogo, los precios, los impuestos y los permisos de acceso. SumRelay
ejecuta el calendario de cobros periódicos.

Cada instalación conecta **una aplicación con un comercio de SumUp**. Utiliza
Django, PostgreSQL, el SDK oficial de SumUp y Docker Compose. Incluye un cliente
Python de servidor y un contrato OpenAPI para integrar otros lenguajes.

## Qué incluye

- Creación idempotente de pagos con importe, moneda y comercio fijados por pedido.
- Verificación del checkout y la transacción antes de confirmar el cobro.
- Estados inciertos explícitos para bloquear reintentos que podrían duplicar un cargo.
- Notificaciones HMAC con cola persistente y reintentos durante 24 horas.
- Conciliación periódica y detección de devoluciones y disputas reportadas por SumUp.
- Alta consentida de tarjetas, tokens cifrados y suscripciones con trazabilidad por vencimiento.
- Panel administrativo con auditoría, cancelación y reintentos de notificaciones.
- Sandbox local, pruebas automatizadas y despliegue con Docker.

## Estado y mantenimiento

[v1.1.0](https://github.com/dreykdrk7/sumrelay/releases/tag/v1.1.0) está publicada
y pasa a mantenimiento. El alcance queda congelado: se priorizan errores de
cobro, seguridad y compatibilidad con el proveedor sobre funciones nuevas.
El piloto de Eventora verificó tokenización, un cobro recurrente, deduplicación
de eventos firmados, devolución total y cancelación con SumUp sandbox real.
Cada instalación necesita sus propias copias y alertas operativas.
Consulta la [política de mantenimiento](docs/MAINTENANCE.md).

## Arranque rápido

Necesitas Git, Python 3.10+ para generar la configuración, Docker con Compose v2
y un comercio sandbox de SumUp para crear pagos de prueba. Los contenedores usan
Python 3.12 y PostgreSQL 17. Los puertos locales 8000 y 8001 deben estar libres.

```sh
git clone https://github.com/dreykdrk7/sumrelay.git
cd sumrelay
scripts/sandbox.sh init
```

Rellena `SUMUP_API_KEY` y `SUMUP_MERCHANT_CODE` del comercio **sandbox** en el
archivo privado `.env.sandbox` y ejecuta:

```sh
scripts/sandbox.sh up
scripts/sandbox.sh check-credentials
```

El panel está en <http://localhost:8000/admin/> y el receptor local en
<http://localhost:8001/>. Las credenciales generadas del panel se guardan en
`.env.sandbox`. Puedes arrancar el panel sin rellenar SumUp; crear pagos seguirá
bloqueado. El helper nunca sustituye un archivo de configuración existente.

La protección sandbox exige que SumUp confirme expresamente ese entorno. Si
omite el campo, `check-connection` comprueba lectura pero no habilita cobros.
En localhost se utiliza polling, porque SumUp no puede enviar callbacks a tu equipo.

```sh
scripts/sandbox.sh status
scripts/sandbox.sh logs
scripts/sandbox.sh down
```

Detener el proyecto conserva los volúmenes. El receptor local es solo una ayuda
de desarrollo y no concede productos ni accesos reales.

## Integrar y desplegar

El [README en inglés](README.md#create-your-first-checkout) incluye una petición
de ejemplo. La [guía de integración](docs/INTEGRATION.md) explica cómo usar el
cliente Python, verificar firmas y conceder un pedido una sola vez. Consulta el
[contrato OpenAPI](docs/openapi.json), la [API](docs/API.md) y la
[documentación de recurrencia](docs/RECURRING.md).

Para instalarlo en tu servidor utiliza `.env.example`, un dominio y tus propios
secretos. La creación de pagos y los cargos recurrentes están desactivados por
defecto. La [guía de operación](docs/OPERATIONS.md) cubre HTTPS, activación,
actualizaciones, copias y restauración.

La vuelta del navegador no prueba el pago. Tu backend debe contrastar referencia,
importe, moneda y comercio con su pedido, exigir un cobro confirmado sin
incertidumbre y registrar la entrega de forma idempotente.

## Estado y colaboración

Es un proyecto comunitario inicial. Las pruebas automatizadas usan datos
sintéticos y no certifican cada comercio o método de pago. Valida tu instalación
en sandbox antes de mover fondos reales.

La entrega de eventos puede repetirse o llegar fuera de orden. Tu aplicación
gestiona facturas, impuestos, consentimiento y acceso. SumRelay detecta devoluciones
iniciadas en SumUp. El estado autenticado del worker está en `/api/v1/operations/status/`.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Las pruebas de concurrencia necesitan PostgreSQL y se ejecutan en CI. Puedes
proponer mejoras mediante issues y pull requests. Lee [CONTRIBUTING.md](CONTRIBUTING.md),
el [roadmap](ROADMAP.md) y [SECURITY.md](SECURITY.md).

## Licencia

[MIT](LICENSE): permite utilizar, modificar y redistribuir el proyecto, también
en productos comerciales, respetando sus términos. Las dependencias conservan
sus propias licencias.

SumRelay es un proyecto comunitario independiente. No es un producto oficial de
SumUp ni está afiliado a la empresa. Las marcas pertenecen a sus titulares.
