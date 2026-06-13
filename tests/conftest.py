"""Shared fixtures para tests unitarios y de integración."""
import os
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock

# Test env vars must be set BEFORE the first `config` import below:
# config.py reads the environment at import time, so a fixture is too late.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("WHATSAPP_TOKEN", "")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "0")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test_telegram_secret")

# Credenciales AWS falsas: con el mock de moto activo (fixture autouse
# dynamo_mock_table) ninguna llamada boto3 debe salir a AWS real, y estas
# credenciales garantizan que un descuido tampoco pueda autenticarse.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import pytest
from fastapi.testclient import TestClient

from config import NEGOCIO, MENSAJES

TEST_USER = "pytest_test_user_001"
TEST_USER2 = "pytest_test_user_002"


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


@pytest.fixture(autouse=True)
def dynamo_mock_table():
    """Tabla DynamoDB simulada con moto, fresca para cada test (issue #16).

    Crea la tabla con el mismo esquema de template.yaml (PK/SK + GSI1 + TTL),
    la siembra con init_db() y la inyecta en los singletons de
    database_dynamo y session_store. Así todo el stack Lambda
    (lambda_handler, chatbot_lambda, session_store, rate_limiter dynamo)
    es testeable sin credenciales AWS y sin tocar la tabla de producción.
    """
    import boto3
    from moto import mock_aws

    import database_dynamo
    import session_store

    with mock_aws():
        dynamo = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamo.create_table(
            TableName=database_dynamo.TABLE_NAME,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        table.wait_until_exists()

        original_db_table = database_dynamo._table
        original_session_table = session_store._table
        database_dynamo._table = table
        session_store._table = table
        try:
            database_dynamo.init_db()
            yield table
        finally:
            database_dynamo._table = original_db_table
            session_store._table = original_session_table


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
