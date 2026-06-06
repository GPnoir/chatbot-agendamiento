"""Base de datos SQLite para agendamiento."""
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from config import HORARIOS_DEFAULT, PROFESIONALES, SERVICIOS

DB_PATH = Path(__file__).parent / "data" / "agendamiento.db"


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS servicios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            duracion_min INTEGER NOT NULL,
            descripcion TEXT DEFAULT '',
            activo BOOLEAN DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS profesionales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            especialidad TEXT DEFAULT '',
            activo BOOLEAN DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS horarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profesional_id INTEGER REFERENCES profesionales(id),
            dia_semana INTEGER NOT NULL,
            hora_inicio TEXT NOT NULL,
            hora_fin TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT,
            telefono TEXT,
            canal TEXT,
            canal_user_id TEXT UNIQUE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS citas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente_id INTEGER REFERENCES clientes(id),
            servicio_id INTEGER REFERENCES servicios(id),
            profesional_id INTEGER REFERENCES profesionales(id),
            fecha DATE NOT NULL,
            hora TEXT NOT NULL,
            estado TEXT DEFAULT 'confirmada',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Seed data si está vacío
    if not conn.execute("SELECT 1 FROM servicios LIMIT 1").fetchone():
        for s in SERVICIOS:
            conn.execute("INSERT INTO servicios (nombre, duracion_min, descripcion) VALUES (?, ?, ?)",
                         (s["nombre"], s["duracion"], s.get("descripcion", "")))
    if not conn.execute("SELECT 1 FROM profesionales LIMIT 1").fetchone():
        for p in PROFESIONALES:
            conn.execute("INSERT INTO profesionales (nombre, especialidad) VALUES (?, ?)",
                         (p["nombre"], p.get("especialidad", "")))
        # Horarios default para cada profesional
        for prof in conn.execute("SELECT id FROM profesionales").fetchall():
            for dia, h in HORARIOS_DEFAULT.items():
                conn.execute("INSERT INTO horarios (profesional_id, dia_semana, hora_inicio, hora_fin) VALUES (?, ?, ?, ?)",
                             (prof["id"], dia, h["inicio"], h["fin"]))
    conn.commit()
    conn.close()


def get_servicios():
    conn = get_db()
    rows = conn.execute("SELECT * FROM servicios WHERE activo = 1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_profesionales():
    conn = get_db()
    rows = conn.execute("SELECT * FROM profesionales WHERE activo = 1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_or_create_cliente(canal: str, canal_user_id: str, nombre: str = None):
    conn = get_db()
    row = conn.execute("SELECT * FROM clientes WHERE canal_user_id = ?", (canal_user_id,)).fetchone()
    if row:
        if nombre and not row["nombre"]:
            conn.execute("UPDATE clientes SET nombre = ? WHERE id = ?", (nombre, row["id"]))
            conn.commit()
            row = conn.execute("SELECT * FROM clientes WHERE id = ?", (row["id"],)).fetchone()
        conn.close()
        return dict(row)
    conn.execute("INSERT INTO clientes (nombre, canal, canal_user_id) VALUES (?, ?, ?)",
                 (nombre, canal, canal_user_id))
    conn.commit()
    row = conn.execute("SELECT * FROM clientes WHERE canal_user_id = ?", (canal_user_id,)).fetchone()
    conn.close()
    return dict(row)


def get_horas_disponibles(profesional_id: int, fecha: date, servicio_duracion: int) -> list[str]:
    """Retorna lista de horas disponibles para un profesional en una fecha, validando solapamiento."""
    dia_semana = fecha.weekday()
    conn = get_db()
    horario = conn.execute(
        "SELECT hora_inicio, hora_fin FROM horarios WHERE profesional_id = ? AND dia_semana = ?",
        (profesional_id, dia_semana)
    ).fetchone()
    if not horario:
        conn.close()
        return []

    # Citas ya agendadas ese día con su duración
    citas = conn.execute(
        """SELECT c.hora, s.duracion_min FROM citas c
           JOIN servicios s ON c.servicio_id = s.id
           WHERE c.profesional_id = ? AND c.fecha = ? AND c.estado = 'confirmada'""",
        (profesional_id, fecha.isoformat())
    ).fetchall()
    conn.close()

    # Construir bloques ocupados (inicio_min, fin_min)
    bloques_ocupados = []
    for cita in citas:
        h, m = map(int, cita["hora"].split(":"))
        inicio_min = h * 60 + m
        bloques_ocupados.append((inicio_min, inicio_min + cita["duracion_min"]))

    # Generar slots cada 30 min y verificar solapamiento
    inicio = datetime.strptime(horario["hora_inicio"], "%H:%M")
    fin = datetime.strptime(horario["hora_fin"], "%H:%M")
    disponibles = []
    current = inicio
    while current + timedelta(minutes=servicio_duracion) <= fin:
        hora_str = current.strftime("%H:%M")
        slot_inicio = current.hour * 60 + current.minute
        slot_fin = slot_inicio + servicio_duracion
        solapa = any(
            slot_inicio < ocu_fin and slot_fin > ocu_inicio
            for ocu_inicio, ocu_fin in bloques_ocupados
        )
        if not solapa:
            disponibles.append(hora_str)
        current += timedelta(minutes=30)
    return disponibles


def get_fechas_disponibles(profesional_id: int, servicio_duracion: int, dias: int = 7) -> list[date]:
    """Retorna fechas con al menos 1 hora disponible en los próximos N días."""
    hoy = date.today()
    fechas = []
    for i in range(1, dias + 1):
        d = hoy + timedelta(days=i)
        if get_horas_disponibles(profesional_id, d, servicio_duracion):
            fechas.append(d)
    return fechas


def crear_cita(cliente_id: int, servicio_id: int, profesional_id: int, fecha: str, hora: str) -> dict:
    conn = get_db()
    conn.execute(
        "INSERT INTO citas (cliente_id, servicio_id, profesional_id, fecha, hora) VALUES (?, ?, ?, ?, ?)",
        (cliente_id, servicio_id, profesional_id, fecha, hora)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM citas WHERE id = last_insert_rowid()").fetchone()
    conn.close()
    return dict(row)


def get_citas_cliente(cliente_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute("""
        SELECT c.*, s.nombre as servicio_nombre, p.nombre as profesional_nombre
        FROM citas c
        JOIN servicios s ON c.servicio_id = s.id
        JOIN profesionales p ON c.profesional_id = p.id
        WHERE c.cliente_id = ? AND c.estado = 'confirmada' AND c.fecha >= date('now')
        ORDER BY c.fecha, c.hora
    """, (cliente_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cancelar_cita(cita_id: int):
    conn = get_db()
    conn.execute("UPDATE citas SET estado = 'cancelada', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (cita_id,))
    conn.commit()
    conn.close()


def modificar_cita(cita_id: int, nueva_fecha: str, nueva_hora: str):
    conn = get_db()
    conn.execute("UPDATE citas SET fecha = ?, hora = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                 (nueva_fecha, nueva_hora, cita_id))
    conn.commit()
    conn.close()
