"""Shared fixtures para tests unitarios y de integración."""
import os
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from config import NEGOCIO, MENSAJES

TEST_USER = "pytest_test_user_001"
TEST_USER2 = "pytest_test_user_002"


@pytest.fixture(autouse=True)
def env_setup():
    """Override env vars para evitar dependencias externas."""
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
    os.environ.setdefault("WHATSAPP_TOKEN", "")
    os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "0")
    os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "mi_token_secreto")


@pytest.fixture
def temp_db_path() -> Generator[Path, None, None]:
    """Crea un archivo de base de datos temporal y sobreescribe DB_PATH."""
    import database as db_module

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = Path(f.name)
    original_path = db_module.DB_PATH
    db_module.DB_PATH = tmp_path
    yield tmp_path
    db_module.DB_PATH = original_path
    if tmp_path.exists():
        tmp_path.unlink()


@pytest.fixture
def fresh_db(temp_db_path: Path):
    """Inicializa BD fresca y retorna el módulo database."""
    import database as db_module

    db_module.init_db()
    yield db_module


@pytest.fixture(autouse=True)
def clear_chatbot_sessions():
    """Limpia sesiones del chatbot y rate limiter entre tests."""
    import chatbot as chatbot_module
    import rate_limiter

    chatbot_module._sessions.clear()
    rate_limiter.reset()
    yield


@pytest.fixture
def mock_whatsapp_send():
    """Mock de send_message de WhatsApp."""
    import channels.whatsapp_bot

    sent: list[dict] = []
    original = channels.whatsapp_bot.send_message

    async def fake_send(to: str, text: str):
        sent.append({"to": to, "text": text})

    channels.whatsapp_bot.send_message = fake_send
    yield sent
    channels.whatsapp_bot.send_message = original


@pytest.fixture
def mock_telegram_startup(temp_db_path):
    """Mock del startup de Telegram para evitar setup externo."""
    import server as server_module
    import database as db_module
    db_module.init_db()
    yield


@pytest.fixture
def client(mock_telegram_startup) -> Generator[TestClient, None, None]:
    """TestClient de FastAPI apuntando al server real (sin Telegram)."""
    import server as server_module
    server_module.app.router.on_startup.clear()
    server_module.app.router.on_shutdown.clear()
    with TestClient(server_module.app) as c:
        yield c
