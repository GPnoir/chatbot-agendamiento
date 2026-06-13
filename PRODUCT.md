# PRODUCT — Centro de Flores de Bach

> Contexto de diseño para el portal admin y la UX conversacional del chatbot.
> Las decisiones visuales viven en [DESIGN.md](DESIGN.md).

## Qué es

Herramienta interna de agendamiento para un único negocio de terapia floral
(flores de Bach). Dos superficies:

1. **Portal admin** (`/admin/panel`) — uso diario de la terapeuta: ver la
   agenda de la semana y revisar el reporte de citas. Es una herramienta de
   trabajo: el diseño **sirve** al contenido, no es el producto.
2. **Chatbot** (Telegram + WhatsApp) — cara al cliente. El "diseño" es copy +
   estructura de mensajes + botones inline. Calmo, claro, humano.

## Audiencia

- **Admin:** Nelly Pailacura, terapeuta floral. Una sola persona, la usa desde
  el teléfono o el computador durante la jornada (luz de día, escritorio o
  consulta). No es usuaria técnica: cero jerga, cero configuración.
- **Cliente:** personas agendando una sesión de bienestar. Esperan calidez y
  simpleza, no un formulario corporativo.

## Voz

Cercana, serena, en español de Chile neutro. Frases cortas. Sin signos de
exclamación apilados ni emoji decorativo de relleno. Un emoji ocasional con
intención (estado, no adorno). Trata de "tú".

## Lane de marca

Botánico sobrio y natural — como el cuaderno de una herborista, no como un spa
de stock ni una app de productividad. Papel limpio, una tinta profunda de
hoja, tipografía con un toque editorial.

## Anti-referencias (qué NO queremos)

- **Verde Material de catálogo** (`#4caf50` y familia). Es el reflejo de IA de
  primer orden para "wellness". Prohibido.
- **Sage sobre crema / beige.** El segundo reflejo, un escalón más profundo.
  Igual de evitado.
- **Look de spa de stock:** degradados suaves, glassmorphism, hojas
  acuareladas, mármol.
- **Dashboard SaaS:** tarjetas-métrica idénticas con número gigante + degradado,
  barras laterales de color, eyebrows en mayúsculas sobre cada sección.

## Decisiones tomadas

- **Paleta:** papel neutro (off-white real, sin tinte cálido) + tinta casi
  negra con sesgo botánico + UN acento verde-hoja profundo (≤10% de la
  superficie) + arcilla apagada para cancelaciones. Estrategia *restrained*
  (default de producto). Ver [DESIGN.md](DESIGN.md).
- **Tipografía:** eje de contraste serif + sans, ambos por stack del sistema
  (cero peso de red, robusto offline). Serif editorial para títulos y cifras
  guía; sans del sistema para UI y cuerpo.
- **Modo:** claro. Herramienta de día, ambientes iluminados.
