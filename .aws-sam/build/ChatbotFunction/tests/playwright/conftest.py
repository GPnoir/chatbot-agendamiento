"""Fixtures para tests con Playwright.

Configura un servidor FastAPI mínimo (sin Telegram) que permite
probar la API y el flujo conversacional vía webhook de WhatsApp.
"""
import asyncio
import os
import threading
from typing import AsyncGenerator, Generator, List

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import APIRequestContext, Playwright

from config import HOST, PORT, WHATSAPP_VERIFY_TOKEN

# ── Módulo de captura para respuestas del chatbot ──────────────────────
chatbot_responses: List[dict] = []


async def _fake_whatsapp_send(to: str, text: str):
    chatbot_responses.append({"to": to, "text": text})


# ── Aplicación de prueba (sin Telegram) ────────────────────────────────
def build_test_app():
    import channels.whatsapp_bot as wb
    import database as db_module

    wb.send_message = _fake_whatsapp_send

    app = FastAPI(title="chatbot-agendamiento-test")
    app.include_router(wb.router)

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "chatbot-agendamiento"}

    @app.on_event("startup")
    async def startup():
        db_module.init_db()

    return app


test_app = build_test_app()


@pytest.fixture(scope="session")
def server_url() -> Generator[str, None, None]:
    """Arranca uvicorn en un hilo de fondo y devuelve la URL."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    actual_port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(test_app, host="127.0.0.1", port=actual_port, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import time
    timeout = 10
    while not server.started and timeout > 0:
        time.sleep(0.2)
        timeout -= 0.2
    if not server.started:
        raise RuntimeError("Server did not start")

    addr = f"http://127.0.0.1:{actual_port}"
    yield addr

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def api_context(
    playwright: Playwright, server_url: str
) -> Generator[APIRequestContext, None, None]:
    """Contexto de API de Playwright apuntando al servidor de prueba."""
    context = playwright.request.new_context(base_url=server_url)
    yield context
    context.dispose()


@pytest.fixture(autouse=True)
def clear_responses():
    import rate_limiter
    import chatbot as chatbot_module
    chatbot_responses.clear()
    rate_limiter.reset()
    chatbot_module._sessions.clear()
    yield
    chatbot_responses.clear()
