# SumRelay para el CRM de Cazorla

El CRM conserva las condiciones aceptadas, el consentimiento y los permisos de
acceso. SumRelay ejecuta el calendario de cobros y verifica el resultado con
SumUp. La facturación y los impuestos corresponden al CRM.

## Alcance de la candidata

- [x] Alta consentida de tarjeta y verificación del mandato antes de cada
  intento de cargo.
- [x] Calendario en UTC con cadencia de 1 a 12 meses, referencia única por
  vencimiento, historial y cancelación persistente.
- [x] Verificación del checkout y la transacción, bloqueo de duplicados y
  conciliación de respuestas perdidas, devoluciones y disputas.
- [x] Eventos HMAC, auditoría por suscripción, panel y supervisión del worker.
- [x] Verificación automatizada en PostgreSQL y migración de intentos históricos.
- [ ] Aceptación con un comercio sandbox de SumUp habilitado para tokenización
  y el receptor HTTPS del CRM.

## Integración y aceptación

1. Configurar un comercio **sandbox** separado y comprobarlo con
   `python manage.py check_sumup --require-sandbox`. Registrar la URL HTTPS
   del receptor del CRM y el retorno exacto de su interfaz.
2. Usar el widget de SumUp para autorizar una tarjeta de prueba. Comprobar
   que el alta devuelve `ready` y `uncertain=false`. Guardar el UUID de la
   tarjeta y la referencia de consentimiento en el CRM.
3. Crear una suscripción de prueba con inicio en la fecha UTC actual. Repetir
   la misma solicitud y comprobar que conserva el UUID. Comparar el cargo
   confirmado en SumUp, SumRelay y el CRM usando fecha, importe, comercio,
   checkout y transacción.
4. Reprocesar el mismo evento firmado en el receptor del CRM. Verificar que
   el vencimiento solo se aplica una vez. Probar una devolución y comprobar
   que el CRM toma el estado actualizado.
5. Probar la cancelación antes del vencimiento y durante un checkout abierto.
   Si el comercio permite simular un rechazo o un desafío de autenticación,
   comprobar que el CRM conserva el caso pendiente y evita un segundo cargo
   hasta que SumUp confirme el cierre.
6. Registrar el resultado del checkout y de la entrega de eventos, junto con
   los IDs de prueba. Los estados de integración y cobro se consultan desde
   el contrato API y el panel de operación.

[Contrato OpenAPI](openapi.json) · [Suscripciones](RECURRING.md#scheduled-subscriptions) · [Operación](OPERATIONS.md)
