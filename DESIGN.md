# DESIGN — Sistema visual

Lenguaje "papel neutro + acento botánico". Sirve al portal admin
(`/admin/panel`) y orienta la copy del chatbot. Contexto en [PRODUCT.md](PRODUCT.md).

## Color (OKLCH)

Tokens vivos en el `:root` de `/admin/panel` ([lambda_handler.py](lambda_handler.py)).
Estrategia **restrained**: neutros tintados + un acento que no pasa del ~10% de
la superficie.

| Token | Valor | Uso |
|---|---|---|
| `--bg` | `oklch(0.985 0.003 200)` | Fondo (papel, off-white casi neutro, leve frío — **no** cálido) |
| `--surface` | `oklch(1 0 0)` | Paneles, grilla |
| `--surface-sunk` | `oklch(0.965 0.004 200)` | Celdas cerradas, pistas de barra |
| `--ink` | `oklch(0.26 0.015 165)` | Texto principal (sesgo hoja) |
| `--ink-2` | `oklch(0.45 0.012 165)` | Texto secundario (≥4.5:1 sobre blanco) |
| `--ink-3` | `oklch(0.48 0.012 165)` | Labels/muted (verificado ≥4.5:1) |
| `--line` / `--line-2` | `oklch(0.91 / 0.85 …)` | Bordes hairline |
| `--accent` | `oklch(0.47 0.082 156)` | Acento botánico: puntos, barras, foco, citas confirmadas |
| `--accent-strong` | `oklch(0.40 0.075 156)` | Hover/activo |
| `--accent-tint` / `-2` | `oklch(0.955 / 0.91 …)` | Fondos suaves (cita, día actual) |
| `--clay` / `--clay-ink` | `oklch(0.55 / 0.45 … 40)` | Cancelaciones (arcilla apagada, **no** rojo de alarma) |

**Reglas de contraste:** todo texto ≥4.5:1; los nombres en las citas usan
`--ink` (no el acento) sobre tint para no lavarse.

## Tipografía

Eje de contraste serif + sans, ambos por stack del sistema (sin web fonts).

- `--font-display`: `"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif` — títulos y cifras guía.
- `--font-ui`: `system-ui,-apple-system,"Segoe UI",Roboto,…,sans-serif` — UI, cuerpo, tablas.

Display: `letter-spacing` ≥ -0.02em, `text-wrap: balance`. Cuerpo ≤ 70ch.

## Forma y espacio

- Radios: `--r-sm 8px`, `--r-md 10px`, `--r-lg 14px`. Tope 14px en paneles;
  pill solo en tags/chips de control.
- Escala de espacio: 4 / 8 / 12 / 16 / 24 / 32 / 48.
- Sombra única y discreta (`--shadow`); **nunca** borde 1px + sombra ≥16px
  juntos como decoración.
- `--z`: backdrop 100 → modal 110. Sin valores mágicos (999).

## Motion

- Curva: `--ease: cubic-bezier(0.22, 1, 0.36, 1)` (ease-out-quint). Sin bounce.
- Cambio de vista Agenda↔Reporte: crossfade de opacidad (~180ms).
- Barras del reporte: crecen de 0 a su ancho con stagger leve al cargar.
- `@media (prefers-reduced-motion: reduce)`: sin transiciones, estado final
  inmediato.

## Bans aplicados (impeccable)

Removidos del diseño anterior y prohibidos a futuro:

- ❌ Side-stripe `border-left` de color en las citas → reemplazado por chip con
  fondo tint + punto de estado.
- ❌ Paleta Material green → acento botánico profundo.
- ❌ Fondo near-white cálido → papel neutro frío.
- ❌ Hero-metric template / tarjetas-métrica idénticas → resumen editorial
  (cifra guía en serif + fila de stats con separadores hairline).
- ❌ Gradient text, glassmorphism decorativo, eyebrows en mayúsculas por sección.

## Componentes

- **Segmented control** (Agenda · Reporte): pill con indicador del activo,
  `aria-selected`, foco visible.
- **Cita (chip):** fondo `--accent-tint`, punto de estado, nombre en `--ink`,
  servicio en `--ink-2`. Cancelada → `--clay-tint` + punto arcilla.
- **Resumen de reporte:** cifra guía (serif) + `<dl>` de stats con separadores.
- **Medidor de tasa:** barra fina horizontal (arcilla) con label.
- **Barras por servicio:** lista con pista + relleno proporcional (acento) + valor.
- **Estados:** login shell, cargando (skeleton), vacío (mensaje sereno + marca),
  error de auth (vuelve al login).
