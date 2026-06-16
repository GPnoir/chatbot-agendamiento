# Chatbot de Agendamiento

## Project Context

Chatbot multicanal (Telegram + WhatsApp) para agendamiento de citas. Desplegado como monolith Lambda con channels pattern. Cliente: Centro de Flores de Bach.

## Stack

- Runtime: Python 3.14 (Lambda uses 3.11)
- Infra: AWS SAM (Lambda + API Gateway + DynamoDB)
- Channels: Telegram Bot API, WhatsApp Cloud API (Meta)
- Testing: pytest 9.0.3 (asyncio_mode=auto)
- CI/CD: GitHub Actions (runners **GitHub-hosted ubuntu**) → SAM deploy
- Database: DynamoDB (local: SQLite fallback via database.py)

## Architecture

- Monolith Lambda with channels pattern
- `channels/base.py` — abstract channel interface
- `channels/telegram_bot.py` — Telegram implementation
- `channels/whatsapp_bot.py` — WhatsApp implementation
- `chatbot.py` — business logic (servicios, horarios, citas)
- `chatbot_lambda.py` — Lambda-specific handler
- `lambda_handler.py` — API Gateway routing + **panel admin web** (`/admin/*`)
- `database_dynamo.py` — DynamoDB persistence (citas, clientes, notas)
- `database.py` — local SQLite persistence
- `config.py` — environment + business config
- `rate_limiter.py` — per-user rate limiting
- `admin_auth.py` — auth del panel admin (hash PBKDF2 + tokens de sesión firmados)
- `telegram_ui.py` — inline keyboards + armado de mensajes (`build_message`, `button_label`)

## Security Rules — NEVER VIOLATE

1. Never log tokens, secrets, or webhook secrets
2. Always validate webhook signatures (Telegram: X-Telegram-Bot-Api-Secret-Token, WhatsApp: X-Hub-Signature-256)
3. Always sanitize user input before processing
4. Never trust user-sent IDs for authorization — verify against channel context
5. Rate limit all user-facing endpoints
6. NoEcho on ALL secrets in template.yaml
7. Never commit .env — only .env.example

## Commands

```bash
# Testing
pytest                                    # All unit tests
pytest tests/test_chatbot_unit.py         # Chatbot logic tests
pytest tests/test_database_unit.py        # Database tests
pytest tests/test_security_and_overlap.py # Security + scheduling overlap
pytest tests/test_server_integration.py   # Integration tests
pytest tests/playwright/                  # E2E tests (Playwright)

# Local dev
python main.py                            # Local server (port 8000)

# Panel admin (auth): generar credenciales
python scripts/hash_admin_password.py     # imprime ADMIN_PASSWORD_HASH + SESSION_SECRET

# Deploy
./deploy.sh                               # SAM build + deploy
sam build                                 # Build only
sam local invoke                          # Test Lambda locally
```

> El deploy real corre en CI (push a `main`); `deploy.sh` es para deploy manual.
> Panel admin: `GET /admin/panel` (login usuario+contraseña).

## Testing Strategy

- TDD strict: write tests FIRST, then implement
- Unit tests: test_*_unit.py (fast, no external deps)
- Integration: test_server_integration.py (local server)
- E2E: Playwright (full flow via real webhook simulation)
- Security: test_security_and_overlap.py (input validation, overlap detection)

## Business Domain

- Servicios: Consulta inicial (60min), Sesión de seguimiento (30min).
  ("Preparación de esencias" se retiró del catálogo; se desactiva vía
  `_sync_catalogo_servicios`, las citas históricas se conservan.)
- Precios por tramo (CLP), snapshot en cada cita (`config.TRAMOS_PRECIO`):
  convenio TEA/TDAH · niño particular · adulto particular. Consulta inicial:
  10.000 / 15.000 / 20.000. Seguimiento: 8.000 / 12.000 / 18.000. La cita nace
  como `adulto`; la terapeuta ajusta el tramo en el panel (`actualizar_tramo_cita`).
- Estados de cita: `confirmada` → `completada` | `no_show` | `cancelada`. La
  terapeuta marca realizada/no-asistió desde el panel (`marcar_estado_cita`);
  `cancelada` libera el slot y borra el evento del calendar.
- Profesional: Terapeuta Nelly Pailacura
- Max citas por cliente: 3
- Channels: Telegram + WhatsApp (same business logic, different adapters)

## Branching

- `main` — producción; cada push dispara deploy (GitHub Actions → SAM)
- `dev` — rama de integración (default); los features salen y vuelven aquí
- Feature branches (`feat/...`, `fix/...`) → PR a `dev`, merge con **squash**
- Release: PR `dev` → `main`, merge con **merge commit** (no squash, para que las historias no diverjan)

## Workflow

1. One feature/fix at a time
2. Write tests FIRST (TDD) — pytest unit + integration
3. Verify no regressions: `pytest` full suite
4. Security implications → update test_security_and_overlap.py
5. Infra changes → update template.yaml + deploy.sh
6. Ask before proceeding to next feature

## Known Security Gaps (Priority)

All initial gaps resolved (June 2026):

1. ~~Rate limiter in-memory~~ → DynamoDB backend (RATE_LIMITER_BACKEND=dynamo)
2. ~~Weak input sanitization~~ → input_validation.py (length, control chars, structural)
3. ~~No body size limits~~ → _BodySizeLimitMiddleware (413 over 1 MiB)
4. ~~No CORS~~ → CORSMiddleware via CORS_ORIGINS config
5. ~~Weak admin auth~~ → login usuario+contraseña (PBKDF2) + token de sesión firmado HMAC (admin_auth.py); ADMIN_API_KEY queda como break-glass para automatización
6. ~~No structured logging~~ → aws-lambda-powertools (observability.py)
7. ~~No alarms~~ → CloudWatch alarms in template.yaml + optional AlarmEmail

## Open Issues (GitHub)

- #20 DynamoDB backup (DONE — PITR enabled in template.yaml)
- #19 Structured logging (DONE — observability.py)
- #18 Custom domain for API Gateway (open — tecnico)
- #17 Monitoring/alarms (DONE — CloudWatch alarms in template.yaml)
- #16 Lambda mock tests (DONE — moto fixture in tests/conftest.py + test_lambda_moto_unit.py)
- #15 Metrics/reports (DONE — /reporte admin command + GET /admin/reporte + **Reporte dashboard UI** en /admin/panel)
- #14 Google Calendar export (DONE — google_calendar.py, commit b816299)
- #13 Payment integration (open — nice-to-have)
- #12 Multi-professional support (open — nice-to-have)
- #11 Smart rescheduling (open — nice-to-have)
- #10 Interactive buttons (DONE — telegram_ui.py + callback_query in webhook)

## Frontend / Diseño

Sistema de diseño "papel neutro + acento botánico" documentado en `PRODUCT.md`
y `DESIGN.md` (tokens OKLCH, tipografía serif+sans del sistema, motion, bans).
Antes de tocar UI, leerlos. Aplica a:

- **Portal admin** (`/admin/panel` en lambda_handler.py): app web client-side con
  **login usuario+contraseña** (sesión firmada, ver `admin_auth.py`) y menú
  hamburguesa con 4 destinos:
  - **Agenda** semanal interactiva (click en una cita → panel de detalle:
    marcar realizada / no asistió, asignar tramo de precio, o cancelar). Muestra
    confirmadas + atendidas (✓ realizada, ✕ no-show); oculta canceladas.
  - **Reporte** (dashboard de métricas sobre `/admin/reporte`).
  - **Fichas** de pacientes (lista + buscador → ficha con histórico de citas,
    notas del terapeuta y **contacto**: link wa.me para WhatsApp y envío de
    mensaje vía el bot, con botones opcionales de reagendar/cancelar).
  - **Cerrar sesión**.

  El shell NO embebe datos ni secretos (contrato cubierto en
  test_security_and_overlap.py). Endpoints admin (todos con auth; **cada uno
  declarado como ruta `Api` en template.yaml** o API Gateway responde 403 en prod):
  `/admin/login`, `/admin/agenda`, `/admin/reporte`, `/admin/cita/cancelar`,
  `/admin/cita/estado`, `/admin/cita/tramo`, `/admin/clientes`, `/admin/cliente`,
  `/admin/cliente/nota`, `/admin/cliente/mensaje`.
- **Chatbot**: la "UI" es copy + estructura de mensajes (config.MENSAJES) +
  botones inline (telegram_ui.py). `build_message` arma texto+teclado **sin
  duplicar** las opciones numeradas; al tocar un botón el mensaje se edita para
  registrar la elección; pasos críticos (cancelar, reagendar) piden confirmación.
  Voz definida en PRODUCT.md.

Para iterar diseño usar la skill `impeccable` (sub-comandos: critique, polish,
craft, ...).

### Bugs de diseño/UX por resolver

- ~~**Opción 5 muerta:** la bienvenida ofrecía "5️⃣ Historial de citas" pero
  `chatbot.py` solo manejaba 1–4.~~ RESUELTO — `chatbot_lambda.py` (producción)
  ya la tenía; se implementó en `chatbot.py` + `database.get_historial_cliente`
  para dejar ambos motores consistentes. Cubierto por TestHistorial
  (test_chatbot_unit.py) y TestCitas (test_database_unit.py).

> Recordatorio: `chatbot.py` (SQLite, dev local) y `chatbot_lambda.py`
> (DynamoDB, prod) son dos motores paralelos — cambios de lógica de negocio
> deben aplicarse en ambos.

## Code Style

- Python: type hints on all public functions
- Docstrings on modules and classes (Spanish OK for business domain)
- Config via environment variables (never hardcode secrets)
- Channel implementations inherit from channels/base.py
- Error handling: log + graceful response to user (never expose internals)
