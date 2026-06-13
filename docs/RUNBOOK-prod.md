# Runbook — Despliegue a producción y validación funcional

Guía operativa para llevar el chatbot a prod y verificar que quedó sano.
Cliente: Centro de Flores de Bach. Stack AWS: `chatbot-agendamiento` (us-east-1).

## 1. Cómo se despliega

El deploy es **automático al hacer push a `main`** (`.github/workflows/ci-cd.yml`):

```
push/merge a main → [tests en runner self-hosted] → sam build → sam deploy
                  → setWebhook Telegram → smoke test /health
```

Componentes (SAM): Lambda `chatbot-agendamiento` (API), Lambda `chatbot-reminder`
(recordatorios cada hora), API Gateway, DynamoDB (PITR + TTL), alarmas CloudWatch,
warm-up cada 5 min.

### Flujo de ramas (ver CLAUDE.md)

1. Feature `feat/...` o `fix/...` → PR a `dev`, merge **squash**.
2. Release: PR `dev` → `main`, merge **commit** (no squash). Ese push a `main`
   dispara el deploy.

## 2. Pre-requisitos

**Secrets en GitHub Actions** (el pipeline los pasa como `--parameter-overrides`,
todos `NoEcho`):

| Secret | ¿Obligatorio? | Para qué |
|---|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Sí | Credenciales del deploy |
| `TELEGRAM_BOT_TOKEN` | Sí | Bot de Telegram |
| `TELEGRAM_WEBHOOK_SECRET` | Sí (MinLength 1) | Valida el webhook; vacío = rechaza todo (403) |
| `WHATSAPP_VERIFY_TOKEN` | Sí (MinLength 1) | Verificación del webhook de Meta |
| `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID` | Sí (si se usa WhatsApp) | Envío de mensajes |
| `WHATSAPP_APP_SECRET` | Recomendado | Valida firma `X-Hub-Signature-256` |
| `ADMIN_API_KEY` | **Sí, para el panel** | Sin esto el panel/Reporte rechaza todo (fail-closed) |
| `ALARM_EMAIL` | Opcional | Notificaciones de alarmas (SNS) |
| `GOOGLE_*` | Opcional | Solo si se activa la sync de Google Calendar |

**Infra:** el runner **self-hosted debe estar online**, cuenta AWS con permisos de
CloudFormation/Lambda/DynamoDB/API Gateway, SAM CLI en el runner.

> **Crítico:** definí `ADMIN_API_KEY` antes de este deploy. Es la única forma de
> entrar al panel admin, y `_check_admin_auth` falla cerrado si está vacío.

## 3. Post-deploy: configuración externa

- **Telegram:** lo hace el pipeline (`setWebhook`). Nada manual.
- **WhatsApp:** configurar **a mano** en Meta Developer Console la URL del webhook
  (output `WhatsAppWebhookUrl`) + el `verify_token`.
- **Alarmas:** si seteaste `ALARM_EMAIL`, confirmá el mail de suscripción SNS que
  manda AWS, o no llegan alertas.

URLs del stack desplegado:

```bash
aws cloudformation describe-stacks --stack-name chatbot-agendamiento \
  --region us-east-1 --query 'Stacks[0].Outputs' --output table
```

## 4. Checklist de validación funcional en prod

`<API>` = output `ApiUrl`.

### Infra / humo
- [ ] `curl https://<API>/health` → `{"status":"ok","service":"chatbot-agendamiento",...}`
- [ ] Telegram conectado: `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
      → `url` correcta y `last_error_message` vacío

### Bot (desde Telegram real)
- [ ] `/start` → menú con las 5 opciones
- [ ] Opción 1 (agendar): servicio → fecha → hora → nombre → confirmar →
      "Cita agendada" + notificación al profesional
- [ ] Botones inline responden
- [ ] Opción 4 (Ver mis citas): muestra las próximas
- [ ] Opción 5 (Historial): lista el historial incluyendo canceladas/pasadas
      (no repite el menú)
- [ ] Opción 3 (cancelar): pide confirmación y cancela

### Panel admin — `https://<API>/admin/panel`
- [ ] Login con el diseño nuevo (marca botánica, papel neutro)
- [ ] Clave incorrecta → rechazada; clave correcta (`ADMIN_API_KEY`) → entra
- [ ] Tab Agenda: grilla semanal con citas reales, columna de hoy resaltada
- [ ] Tab Reporte: total / confirmadas / canceladas / tasa / barras por servicio
      (sin spinner infinito ni error)
- [ ] Ver-fuente del HTML: no hay citas ni la API key embebidas (login shell)

### Seguridad (deberían fallar)
- [ ] `curl https://<API>/admin/agenda` (sin header) → 401
- [ ] `curl -H "Authorization: Bearer <ADMIN_API_KEY>" https://<API>/admin/reporte`
      → 200 JSON (confirma que la ruta de API Gateway está wireada; antes daba 403)
- [ ] Webhook Telegram sin el header secreto → 403

### Programados / observabilidad
- [ ] `ReminderFunction` corre cada hora (CloudWatch Logs `/aws/lambda/chatbot-reminder`)
- [ ] Alarmas creadas en CloudWatch; si `ALARM_EMAIL`, suscripción SNS confirmada
- [ ] Warm-up cada 5 min mantiene la Lambda caliente

### Google Calendar (solo si está activado)
- [ ] Agendar una cita → aparece el evento en el calendario configurado
- [ ] Cancelar esa cita → el evento se borra

## 5. Rollback

- Re-deploy del commit anterior: revertí el merge en `main` → el push redispara
  el pipeline con la versión previa.
- DynamoDB tiene **PITR** (point-in-time recovery) por si hay que restaurar datos.

## 6. Notas / gotchas

- `GET /admin/reporte` debe estar declarado como ruta de API Gateway en
  `template.yaml` (evento `AdminReporte`). Sin esa ruta, la vista Reporte recibe
  403 de API Gateway aunque el endpoint exista en FastAPI. Cubierto por el test
  `test_template_rutea_admin_reporte`.
- El runner self-hosted debe estar online o el deploy no corre.
- Hay dos motores conversacionales: `chatbot.py` (SQLite, dev local) y
  `chatbot_lambda.py` (DynamoDB, prod). Los cambios de lógica van en ambos.
