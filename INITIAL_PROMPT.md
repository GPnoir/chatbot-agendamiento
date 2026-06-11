# Initial Prompt — Security Hardening Sprint

Copy this as your FIRST message in Claude Code (Fable 5 + Ultracode):

---

I need to harden the security of this chatbot before adding new features. The CLAUDE.md has all the context. Here's what needs to happen in priority order:

## Phase 1: Critical Security Fixes

1. **Persistent rate limiter** — Replace the in-memory rate_limiter.py with a DynamoDB-backed implementation. The current one resets on every Lambda cold start, making it useless. Use the existing ChatbotTable with a new PK pattern like `RATELIMIT#<user_id>` and TTL for automatic cleanup.

2. **Input validation middleware** — Create a proper input validation layer:
   - Enforce message length (max 500 chars) BEFORE processing
   - Strip null bytes, control characters
   - Validate webhook payload structure (JSON schema validation)
   - Add request body size limit to FastAPI (max 1MB)

3. **CORS configuration** — Add explicit CORS middleware to FastAPI. Only allow the API Gateway origin in production.

4. **Admin endpoint auth** — Replace the shared TELEGRAM_WEBHOOK_SECRET token with a dedicated admin API key (new env var ADMIN_API_KEY with separate SAM parameter).

## Phase 2: Observability (Issues #19, #17)

5. **Structured logging** — Replace all print/logger.info with structured JSON logging (aws_lambda_powertools or equivalent). Include request_id, channel, user_id (hashed), action, and duration.

6. **CloudWatch alarms** — Add to template.yaml: Lambda error rate alarm, DynamoDB throttle alarm, and 4xx/5xx API Gateway alarms. Output the alarm ARNs.

## Phase 3: Test Infrastructure (Issue #16)

7. **Moto-based Lambda tests** — Add tests that mock DynamoDB with moto to test database_dynamo.py without real AWS. Create tests/test_dynamo_moto.py.

## Rules
- TDD: write tests FIRST for each change, then implement
- Run `pytest` after each phase to verify no regressions
- Update template.yaml for any infra changes
- Commit after each phase with conventional commits
- DO NOT touch business logic (chatbot.py) — security layer only

Start with Phase 1. Show me the plan before implementing.
