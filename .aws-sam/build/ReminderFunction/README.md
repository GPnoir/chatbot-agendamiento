# 🤖 Chatbot de Agendamiento

Chatbot conversacional para gestión de citas en negocios de salud. Permite a clientes **agendar, modificar, cancelar y consultar** citas a través de **Telegram** y **WhatsApp**.

Diseñado genérico y fácilmente personalizable. Incluido como ejemplo: un centro de Flores de Bach.

## ✨ Funcionalidades

- 📅 Agendar citas con selección de servicio, profesional, fecha y hora
- ✏️ Modificar citas existentes (nueva fecha/hora)
- ❌ Cancelar citas con confirmación
- 📋 Ver citas pendientes
- 🔀 Multi-canal: Telegram + WhatsApp (misma lógica)
- 👥 Multi-usuario: sesiones independientes por usuario

## 🏗️ Arquitectura

```
Telegram Bot API ──┐
                   ├──→ FastAPI Server → Chatbot Engine → SQLite
WhatsApp Meta API──┘
```

## 📁 Estructura del Proyecto

```
chatbot-agendamiento/
├── config.py              # Configuración del negocio y tokens
├── database.py            # Modelos y operaciones SQLite
├── chatbot.py             # Motor conversacional (máquina de estados)
├── channels/
│   ├── base.py            # Clase abstracta de canal
│   ├── telegram_bot.py    # Adaptador Telegram
│   └── whatsapp_bot.py    # Adaptador WhatsApp (Meta Cloud API)
├── server.py              # FastAPI (modo webhook/producción)
├── main.py                # Punto de entrada
├── requirements.txt       # Dependencias
├── pyproject.toml         # Configuración pytest
├── .env.example           # Variables de entorno requeridas
├── tests/
│   ├── conftest.py        # Fixtures compartidos
│   ├── test_chatbot_unit.py       # 29 tests unitarios del chatbot
│   ├── test_database_unit.py      # 21 tests unitarios de BD
│   ├── test_server_integration.py # 12 tests de integración
│   └── playwright/
│       ├── conftest.py            # Fixtures Playwright
│       ├── test_api_health.py     # 3 tests API health
│       ├── test_api_webhook.py    # 4 tests webhook verification
│       └── test_chat_flow.py      # 12 tests E2E chat
└── data/
    └── agendamiento.db    # SQLite (se crea automáticamente)
```

## 🚀 Instalación

```bash
git clone https://github.com/GPnoir/chatbot-agendamiento.git
cd chatbot-agendamiento
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## ⚙️ Configuración

### 1. Telegram Bot

1. Buscar `@BotFather` en Telegram
2. Enviar `/newbot` y seguir instrucciones
3. Copiar el token en `.env` → `TELEGRAM_BOT_TOKEN`

### 2. WhatsApp (Meta Cloud API)

1. Crear app en [Meta for Developers](https://developers.facebook.com/)
2. Agregar producto WhatsApp
3. Copiar `Phone Number ID` y `Access Token` en `.env`
4. Configurar webhook URL: `https://tu-dominio.com/whatsapp/webhook`

### 3. Variables de entorno

```env
TELEGRAM_BOT_TOKEN=tu_token
WHATSAPP_TOKEN=tu_access_token
WHATSAPP_PHONE_NUMBER_ID=tu_phone_number_id
WHATSAPP_VERIFY_TOKEN=un_string_secreto
HOST=0.0.0.0
PORT=8000
WEBHOOK_URL=https://tu-dominio.com
```

## ▶️ Ejecución

```bash
# Desarrollo (Telegram polling + servidor WhatsApp)
python3 main.py

# Producción (ambos via webhook)
python3 main.py --mode webhook
```

Para desarrollo local con WhatsApp, exponer puerto con ngrok:

```bash
ngrok http 8000
```

## 🧪 Tests

**81 tests** organizados en 3 niveles:

| Tipo | Archivo | Tests | Qué valida |
|------|---------|-------|------------|
| Unitarios | `test_chatbot_unit.py` | 29 | Máquina de estados, transiciones, multi-usuario |
| Unitarios | `test_database_unit.py` | 21 | CRUD, disponibilidad, slots, seed data |
| Integración | `test_server_integration.py` | 12 | Endpoints HTTP, webhooks, ciclos completos |
| API E2E | `playwright/test_api_health.py` | 3 | GET /health |
| API E2E | `playwright/test_api_webhook.py` | 4 | Verificación webhook WhatsApp |
| E2E Chat | `playwright/test_chat_flow.py` | 12 | Flujo completo como usuario real |

### Ejecutar tests

```bash
source venv/bin/activate

# Todos los tests
pytest tests/ -v

# Solo unitarios + integración (rápido)
pytest tests/test_chatbot_unit.py tests/test_database_unit.py tests/test_server_integration.py -v

# Solo Playwright E2E
pytest tests/playwright/ -v
```

### Dependencias de testing

```bash
pip install pytest pytest-asyncio pytest-playwright
python3 -m playwright install chromium
```

## 🎨 Personalización

Para adaptar a otro negocio, solo editar `config.py`:

```python
NEGOCIO = {"nombre": "Mi Negocio de Salud"}

SERVICIOS = [
    {"nombre": "Consulta", "duracion": 60, "descripcion": "..."},
]

PROFESIONALES = [
    {"nombre": "Dr. Nombre", "especialidad": "Especialidad"},
]
```

Ver ejemplos para psicólogo, nutricionista y otros en la documentación.

## 🛠️ Stack

- **Python 3.11+**
- **FastAPI** + uvicorn
- **python-telegram-bot** v21
- **httpx** (Meta Cloud API)
- **SQLite** (sin servidor de BD)
- **pytest** + **Playwright** (testing)

## 📄 Licencia

MIT
