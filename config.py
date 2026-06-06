"""Configuración del negocio - Editar aquí para personalizar."""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Tokens ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "mi_token_secreto")
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
    {"nombre": "Dra. María López", "especialidad": "Terapeuta floral"},
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
    "bienvenida": "¡Hola! 🌸 Soy el asistente de {nombre}.\n¿Qué deseas hacer?\n\n1️⃣ Agendar una hora\n2️⃣ Modificar una cita\n3️⃣ Cancelar una cita\n4️⃣ Ver mis citas",
    "despedida": "¡Gracias! 🌿 Te esperamos.",
    "cita_confirmada": "✅ Cita agendada:\n📋 {servicio}\n👩‍⚕️ {profesional}\n📅 {fecha} a las {hora}",
    "cita_cancelada": "❌ Cita del {fecha} a las {hora} cancelada.",
    "error": "No entendí tu respuesta. Por favor elige una opción válida.",
}
