# WhatsApp: templates y la ventana de 24 horas

## Por qué existe esto

Meta divide los mensajes salientes de WhatsApp en dos mundos:

- **Dentro de la ventana de servicio de 24 h** (contando desde el último
  mensaje que envió el paciente): podés mandar **texto libre** y botones
  interactivos. Es gratis. Es lo que hace el bot al responder un chat.
- **Fuera de esa ventana**: Meta **solo acepta plantillas (templates)
  pre-aprobadas**. El texto libre se rechaza con el error **131047**
  ("Re-engagement message").

El **recordatorio de cita** se envía de forma proactiva ~24 h antes, cuando el
paciente casi nunca escribió en las últimas 24 h. Por eso, sin un template
aprobado, el recordatorio **rebota** (y antes lo hacía en silencio: se marcaba
como enviado aunque no llegara). Este documento explica cómo crear el template
y activarlo.

## Paso 1 — Crear el template en Meta

1. Entrá a **WhatsApp Manager → Plantillas de mensajes → Crear plantilla**.
2. Categoría: **Utility (Utilidad)**. *No* Marketing — utility es más barato
   (~US$0.02 en Chile) y se aprueba más fácil para recordatorios.
3. Nombre: `recordatorio_cita` (solo minúsculas, números y `_`).
4. Idioma: **Español** (código `es`). Si querés `es_CL`, ajustá también la env
   var `WHATSAPP_TEMPLATE_LANG`.
5. Cuerpo (body) — **exactamente 2 variables**, en este orden:

   ```
   Hola 🌿 Te recordamos tu cita de {{1}}: {{2}}. ¿Confirmás tu asistencia? Respondé SÍ o NO a este mensaje.
   ```

   - `{{1}}` = servicio (ej. "Consulta inicial")
   - `{{2}}` = cuándo (ej. "2026-07-05 a las 15:00")

   Meta te va a pedir **valores de ejemplo** para cada variable: usá los de
   arriba.
6. Enviá a revisión. La aprobación tarda de minutos a ~24 h.

> ⚠️ El **orden y la cantidad** de variables tienen que coincidir con el código
> ([reminder_handler.py](../reminder_handler.py), función `handler`). Si cambiás
> el body para usar 3 variables, hay que actualizar también el código y sus
> tests (`tests/test_reminder_whatsapp_unit.py`).

## Paso 2 — Activar el template (variables de entorno)

Una vez **aprobado**, seteá los parámetros del stack (CI o `deploy.sh`):

| Parámetro CloudFormation | Env var | Valor |
|---|---|---|
| `WhatsAppReminderTemplate` | `WHATSAPP_REMINDER_TEMPLATE` | `recordatorio_cita` |
| `WhatsAppTemplateLang` | `WHATSAPP_TEMPLATE_LANG` | `es` |

Mientras `WHATSAPP_REMINDER_TEMPLATE` esté **vacío**, el recordatorio cae al
comportamiento anterior (texto libre) — útil en dev/local o mientras el
template no está aprobado. Apenas lo seteás, el recordatorio pasa a usar el
template.

En CI, agregá el override en `.github/workflows/ci-cd.yml` junto al resto de
`--parameter-overrides` (o como secret si preferís no versionarlo; no es
secreto, así que un valor plano está bien).

## Qué NO necesita template

- **Respuestas del chat** (el paciente escribe → el bot responde): siempre
  dentro de la ventana. Texto libre, gratis.
- **Mensajes desde el panel** (`/admin/cliente/mensaje`): el texto es libre y
  arbitrario, no se puede "templatizar". Si el paciente no escribió en 24 h, el
  envío se rechaza y el panel ahora muestra un aviso claro
  ("pedile que responda para reabrir la conversación") en vez de fallar en
  silencio.

## Cómo lo maneja el código

- `reminder_handler.send_whatsapp_template()` arma el payload `type: template`.
- `reminder_handler._check_meta_response()` / `lambda_handler._check_wa_response()`
  revisan la respuesta de Meta y lanzan `WhatsAppSendError` en `>=400`. Nunca
  loguean el token ni el payload, solo el `code`/`message` de Meta.
- El recordatorio **solo se marca como enviado** (`recordatorio_enviado`) si
  Meta aceptó; si rebota, se reintenta en la siguiente corrida horaria.
- Los códigos de "fuera de ventana" (131047, 131051, 470) se traducen a un
  mensaje entendible en el panel (HTTP 409 `outside_window`).
