"""Configuración del negocio - Editar aquí para personalizar."""
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # En Lambda no hay python-dotenv

# --- Runtime ---
IS_LAMBDA = bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
DYNAMODB_TABLE = os.getenv("DYNAMODB_TABLE", "chatbot-agendamiento")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "1569695377")  # Telegram ID del profesional
MAX_CITAS_POR_CLIENTE = int(os.getenv("MAX_CITAS_POR_CLIENTE", "3"))

# --- Tokens ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "telegram_secret_change_me")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "mi_token_secreto")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "http://localhost:8000")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# --- Negocio ---
NEGOCIO = {
    "nombre": "Centro de Flores de Bach",
    "descripcion": "Terapia floral personalizada",
}

SERVICIOS = [
    {"nombre": "Consulta inicial", "duracion": 60, "descripcion": "Evaluación completa y primera fórmula"},
    {"nombre": "Sesión de seguimiento", "duracion": 30, "descripcion": "Control y ajuste de fórmula"},
    {"nombre": "Preparación de esencias", "duracion": 45, "descripcion": "Preparación personalizada sin consulta"},
]

PROFESIONALES = [
    {"nombre": "Terapeuta Nelly Pailacura", "especialidad": "Terapeuta floral"},
]

HORARIOS_DEFAULT = {
    0: {"inicio": "09:00", "fin": "18:00"},  # Lunes
    1: {"inicio": "09:00", "fin": "18:00"},  # Martes
    2: {"inicio": "09:00", "fin": "18:00"},  # Miércoles
    3: {"inicio": "09:00", "fin": "18:00"},  # Jueves
    4: {"inicio": "09:00", "fin": "17:00"},  # Viernes
    5: {"inicio": "09:00", "fin": "13:00"},  # Sábado
    # 6: Domingo cerrado
}

MENSAJES = {
    "bienvenida": "¡Hola! 🌸 Soy el asistente de {nombre}.\n¿Qué deseas hacer?\n\n1️⃣ Agendar una hora\n2️⃣ Modificar una cita\n3️⃣ Cancelar una cita\n4️⃣ Ver mis citas\n5️⃣ Historial de citas",
    "despedida": "¡Gracias! 🌿 Te esperamos.",
    "cita_confirmada": "✅ Cita agendada:\n📋 {servicio}\n👩‍⚕️ {profesional}\n📅 {fecha} a las {hora}",
    "cita_cancelada": "❌ Cita del {fecha} a las {hora} cancelada.",
    "error": "No entendí tu respuesta. Por favor elige una opción válida.",
}
