# Chatbot de Agendamiento

## Project Context

Chatbot multicanal (Telegram + WhatsApp) para agendamiento de citas. Desplegado como monolith Lambda con channels pattern. Cliente: Centro de Flores de Bach.

## Stack

- Runtime: Python 3.14 (Lambda uses 3.11)
- Infra: AWS SAM (Lambda + API Gateway + DynamoDB)
- Channels: Telegram Bot API, WhatsApp Cloud API (Meta)
- Testing: pytest 9.0.3 (asyncio_mode=auto)
- CI/CD: GitHub Actions → SAM deploy
- Database: DynamoDB (local: SQLite fallback via database.py)

## Architecture

- Monolith Lambda with channels pattern
- `channels/base.py` — abstract channel interface
- `channels/telegram_bot.py` — Telegram implementation
- `channels/whatsapp_bot.py` — WhatsApp implementation
- `chatbot.py` — business logic (servicios, horarios, citas)
- `chatbot_lambda.py` — Lambda-specific handler
- `lambda_handler.py` — API Gateway event routing
- `database_dynamo.py` — DynamoDB persistence
- `database.py` — local SQLite persistence
- `config.py` — environment + business config
- `rate_limiter.py` — per-user rate limiting

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

# Deploy
./deploy.sh                               # SAM build + deploy
sam build                                 # Build only
sam local invoke                          # Test Lambda locally
```

## Testing Strategy

- TDD strict: write tests FIRST, then implement
- Unit tests: test_*_unit.py (fast, no external deps)
- Integration: test_server_integration.py (local server)
- E2E: Playwright (full flow via real webhook simulation)
- Security: test_security_and_overlap.py (input validation, overlap detection)

## Business Domain

- Servicios: Consulta inicial (60min), Seguimiento (30min), Preparación esencias (45min)
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
5. ~~Weak admin auth~~ → Bearer ADMIN_API_KEY with hmac.compare_digest
6. ~~No structured logging~~ → aws-lambda-powertools (observability.py)
7. ~~No alarms~~ → CloudWatch alarms in template.yaml + optional AlarmEmail

## Open Issues (GitHub)

- #20 DynamoDB backup (DONE — PITR enabled in template.yaml)
- #19 Structured logging (DONE — observability.py)
- #17 Monitoring/alarms (DONE — CloudWatch alarms in template.yaml)
- #16 Lambda mock tests (DONE — moto fixture in tests/conftest.py + test_lambda_moto_unit.py)
- #15 Metrics/reports (DONE — /reporte admin command + GET /admin/reporte)
- #14 Google Calendar export
- #13 Payment integration
- #12 Multi-professional support
- #11 Smart rescheduling
- #10 Interactive buttons (DONE — telegram_ui.py + callback_query in webhook)

## Code Style

- Python: type hints on all public functions
- Docstrings on modules and classes (Spanish OK for business domain)
- Config via environment variables (never hardcode secrets)
- Channel implementations inherit from channels/base.py
- Error handling: log + graceful response to user (never expose internals)
