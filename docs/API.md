# API HTTP v1

Contrato procesable: [OpenAPI 3.1.1](openapi.json), disponible también en `GET /api/v1/openapi.json` con autenticación de servidor. Cliente Python y ejemplos para Django + Next.js: [guía de integración](INTEGRATION.md).

Todas las rutas del SaaS requieren HTTPS en producción y la cabecera `Authorization: Bearer <clave de servidor>`. No envíes esa clave al navegador. Las respuestas usan `Cache-Control: no-store`. Los importes de entrada y salida son cadenas decimales, nunca números JSON.

## Crear o solicitar otro intento

`POST /api/v1/payments/`

Cabeceras: `Content-Type: application/json` e `Idempotency-Key: <identificador único>` (1–128 caracteres: letras, números, `.`, `_`, `:`, `-`).

```json
{
  "reference": "ORDER-42",
  "amount": "19.95",
  "currency": "EUR",
  "description": "Pedido 42",
  "return_url": "https://saas.example.com/payment-result"
}
```

Se exigen exactamente esos cinco campos. La referencia distingue mayúsculas y minúsculas, admite hasta 120 caracteres, y la descripción hasta 500. El retorno debe coincidir exactamente con una URL configurada. EUR está habilitado por defecto; el administrador puede habilitar monedas de la lista admitida por el SDK. El importe máximo es `999999999.99`; CLP no admite decimales y las demás monedas habilitadas admiten hasta dos. La aceptación final de moneda e importe depende también del comercio SumUp.

La misma clave y los mismos datos normalizados recuperan el mismo intento. Cambiar los datos con esa clave devuelve `409 idempotency_conflict`. Una clave nueva solicita un intento nuevo: solo se permite si todos los anteriores están cerrados, comprobados y ninguno fue cobrado, incluso si se devolvió después. La referencia conserva su importe, moneda y comercio: cambiarlos devuelve `409 order_conflict`.

Respuesta: `201` para un intento creado y verificado, `200` para una repetición verificada y `202` cuando queda incertidumbre. `202` significa que el intento está registrado y debe consultarse; no autoriza a repetir el POST con otra clave. La cabecera `Location` apunta al pago.

Ejemplo abreviado (la respuesta completa añade fechas y todos los intentos):

```json
{
  "id": "a82aad37-7fbf-441c-b1df-5f5b8d4226ed",
  "reference": "ORDER-42",
  "amount": "19.95",
  "currency": "EUR",
  "merchant_code": "MTEST123",
  "status": "pending",
  "uncertain": false,
  "checkout_url": "https://checkout.sumup.com/pay/checkout-id",
  "latest_attempt_id": "cd4edbf0-d7d3-43ce-9f42-04143327b1f3",
  "refunded_amount": "0.00",
  "requested_attempt": {
    "id": "cd4edbf0-d7d3-43ce-9f42-04143327b1f3",
    "status": "pending",
    "uncertain": false,
    "checkout_url": "https://checkout.sumup.com/pay/checkout-id"
  },
  "attempts": []
}
```

**Para redirigir tras un POST, usa `requested_attempt.checkout_url`.** En una repetición antigua, `requested_attempt` sigue identificando el intento de esa clave aunque el pedido tenga después otro intento. Comprueba primero que el pago no tiene ningún cobro confirmado, que `uncertain=false` y que el intento solicitado sigue abierto. Los campos de nivel superior priorizan un cobro confirmado, aunque corresponda a un intento anterior; sin cobros, describen el más reciente. La incertidumbre agrega todos los intentos y también señala varios cobros o un cobro con otro checkout todavía abierto. No se debe abrir un checkout de un intento cerrado, incierto o ya cobrado.

Cada intento incluye su instantánea financiera, estado, `uncertain`, `closed_confirmed`, `sumup_checkout_id`, `sumup_checkout_reference`, `transaction_id`, `transaction_code`, `refunded_amount`, `refund_events`, `paid_at`, `created_at`, `updated_at`, `expires_at`, `last_checked_at` y `last_error`. La referencia del checkout en SumUp es el UUID del intento; la referencia comercial permanece en `reference`.

## Consultar

- `GET /api/v1/payments/{id}/`
- `GET /api/v1/payments/?reference=ORDER-42` (referencia exacta; codifica los caracteres de URL).

Devuelven el pago con todos sus intentos y eventos de devolución, sin `requested_attempt`. La consulta no llama a SumUp: muestra la última comprobación persistida. El worker y las acciones del administrador actualizan esa información.

Estados: `pending`, `paid`, `failed`, `expired`, `cancelled`, `partially_refunded`, `refunded`, `disputed`. `uncertain=true` conserva el estado previo pero impide tratarlo como una confirmación nueva.

Antes de entregar el producto, el servidor del SaaS debe comparar la referencia, importe, moneda y `merchant_code` con su pedido original; exigir un cobro confirmado sin incertidumbre y procesar la entrega una sola vez en su propia transacción. Un estado de devolución requiere aplicar la política comercial correspondiente. Nunca uses parámetros del retorno del navegador como prueba del cobro.

## Suscripciones y operación

Consulta [el calendario y el historial de suscripciones](RECURRING.md#scheduled-subscriptions)
para crear y cancelar renovaciones. Los pagos programados incluyen `subscription_id` y
`scheduled_for`. El estado autenticado del worker y sus colas se consulta en
`GET /api/v1/operations/status/`.

## Errores

```json
{"error": {"code": "attempt_blocked", "message": "Existe un intento cobrado o sin cerrar."}}
```

| HTTP | Códigos principales |
| --- | --- |
| 400 | `invalid_request`, `invalid_json`, `invalid_idempotency_key`, `invalid_amount`, `invalid_currency`, `return_url_not_allowed`, `reference_required` |
| 401 | `unauthorized` |
| 404 | `not_found` |
| 409 | `idempotency_conflict`, `order_conflict`, `attempt_blocked` |
| 415 | `unsupported_media_type` |
| 503 | `installation_not_configured`, `provider_not_configured`, `payments_disabled`, `recurring_disabled` |

El proxy puede rechazar antes cuerpos demasiado grandes. La aplicación limita el cuerpo a 16 KiB. Los errores del proveedor quedan en el intento como incertidumbre y no se exponen cuerpos de error de SumUp.

## Notificaciones al SaaS

POST al destino configurado, con cuerpo JSON:

```json
{
  "id": "f9bbb3a3-4ae9-4ca0-ad93-834b2b9d2e83",
  "type": "payment.updated",
  "created_at": "2026-11-01T12:00:00+00:00",
  "payment": {"id": "...", "reference": "ORDER-42", "status": "paid", "uncertain": false}
}
```

El objeto `payment` es una instantánea completa de la consulta. Incluye también cambios a pendiente, incertidumbre, devoluciones y disputas. El cuerpo y el ID permanecen estables en cada reintento. El destino queda fijado al crear el evento; cambiar la configuración afecta a eventos nuevos.

Cabeceras:

- `X-Webhook-Id`: ID del evento.
- `X-Webhook-Timestamp`: segundos Unix de este envío.
- `X-Webhook-Signature`: `v1=<HMAC-SHA256 hexadecimal>`.

Firma sobre los bytes exactos `timestamp + "." + cuerpo`, con `SAAS_WEBHOOK_SECRET`. Verifica la firma en tiempo constante, comprueba una tolerancia de reloj (por ejemplo, cinco minutos), compara el ID de cabecera con el del cuerpo y deduplica el evento en almacenamiento persistente. No vuelvas a serializar JSON antes de verificar.

```python
import hashlib
import hmac
import time


def verify(raw_body, timestamp, signature, secret):
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    expected = (
        "v1="
        + hmac.new(
            secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature)
```

Responde `2xx` después de guardar el evento de forma duradera. Las redirecciones no cuentan como entrega. Si se pierde una respuesta, el evento se repite; la entrega comercial debe ser idempotente. Las notificaciones pueden llegar fuera de orden: evita hacer retroceder un pedido por una instantánea antigua y consulta la API para confirmar su situación actual.

Los reintentos usan esperas de 1 min, 5 min, 15 min y luego 1 h, hasta alcanzar 24 horas desde la creación. Al agotarse el plazo quedan visibles en el panel. Un administrador puede abrir otro plazo de 24 horas conservando el ID.

## Callback de SumUp y salud

`POST /webhooks/sumup/` recibe `CHECKOUT_STATUS_CHANGED` y el `id` del checkout. No usa la credencial del SaaS: es una pista no fiable que solo solicita una verificación con SumUp. Devuelve un `204` vacío y descarta eventos desconocidos. Las repeticiones se agrupan mientras haya una comprobación pendiente.

`GET /health/` comprueba la conexión a PostgreSQL; devuelve `200` o `503`. No garantiza que el worker esté funcionando ni que SumUp sea accesible.
