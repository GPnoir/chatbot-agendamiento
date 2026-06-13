"""Construcción de inline keyboards de Telegram desde el texto del bot.

Funciones puras sin dependencia de python-telegram-bot, usables tanto por
el Lambda (que llama a la Bot API vía HTTP) como por el bot local de
desarrollo. El teclado se deriva del propio texto de respuesta, así la
lógica de negocio (chatbot_lambda/chatbot) no necesita cambios: cada línea
"N️⃣ etiqueta" se vuelve un botón cuyo callback_data es el número N, y una
pregunta "(si/no)" se vuelve botones Sí/No.
"""
import re
from typing import Optional

# Línea de opción numerada: dígitos + keycap (U+FE0F opcional + U+20E3)
_OPTION_LINE_RE = re.compile("^(\\d+)\\ufe0f?\\u20e3\\s+(.+)$")
_SI_NO_RE = re.compile(r"\(si/no\)", re.IGNORECASE)

# Límite de Telegram para el texto visible de un botón
_MAX_BUTTON_TEXT = 64
# Etiquetas cortas (horas como "13:30") se agrupan de a 3 por fila
_SHORT_LABEL_LEN = 12
_SHORT_PER_ROW = 3


def build_reply_markup(text: str) -> Optional[dict]:
    """Deriva un reply_markup de inline keyboard desde el texto de respuesta.

    Retorna el dict listo para enviar a la Bot API, o None cuando el texto
    no contiene opciones seleccionables.
    """
    if not text:
        return None

    buttons = []
    for line in text.splitlines():
        m = _OPTION_LINE_RE.match(line.strip())
        if m:
            label = m.group(2).strip()[:_MAX_BUTTON_TEXT]
            buttons.append({"text": label, "callback_data": m.group(1)})

    if buttons:
        if all(len(b["text"]) <= _SHORT_LABEL_LEN for b in buttons):
            rows = [
                buttons[i:i + _SHORT_PER_ROW]
                for i in range(0, len(buttons), _SHORT_PER_ROW)
            ]
        else:
            rows = [[b] for b in buttons]
        return {"inline_keyboard": rows}

    if _SI_NO_RE.search(text):
        return {
            "inline_keyboard": [[
                {"text": "✅ Sí", "callback_data": "si"},
                {"text": "❌ No", "callback_data": "no"},
            ]]
        }
    return None
