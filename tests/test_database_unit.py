"""Tests unitarios de la capa de base de datos (database.py).

Usa una base de datos SQLite temporal para cada test.
"""
import sqlite3
from datetime import date, datetime

import pytest

from tests.conftest import TEST_USER

DIA_SEMANA_HOY = date.today().weekday()


class TestInitDB:
    def test_init_crea_tablas(self, fresh_db):
        """init_db crea las tablas correctamente."""
        conn = fresh_db.get_db()
        tablas = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        nombres = [r["name"] for r in tablas]
        assert "servicios" in nombres
        assert "profesionales" in nombres
        assert "horarios" in nombres
        assert "clientes" in nombres
        assert "citas" in nombres
        conn.close()

    def test_init_siembra_datos(self, fresh_db):
        """init_db inserta datos semilla si la BD está vacía."""
        servicios = fresh_db.get_servicios()
        assert len(servicios) > 0
        profesionales = fresh_db.get_profesionales()
        assert len(profesionales) > 0

    def test_init_idempotente(self, fresh_db):
        """init_db se puede llamar múltiples veces sin errores."""
        for _ in range(3):
            fresh_db.init_db()
        servicios = fresh_db.get_servicios()
        assert len(servicios) > 0


class TestServicios:
    def test_get_servicios_retorna_lista(self, fresh_db):
        servicios = fresh_db.get_servicios()
        assert isinstance(servicios, list)
        assert len(servicios) >= 1

    def test_servicios_tienen_campos_esperados(self, fresh_db):
        servicio = fresh_db.get_servicios()[0]
        assert "id" in servicio
        assert "nombre" in servicio
        assert "duracion_min" in servicio
        assert "activo" in servicio

    def test_servicio_inactivo_no_aparece(self, fresh_db):
        conn = fresh_db.get_db()
        conn.execute("UPDATE servicios SET activo = 0 WHERE id = 1")
        conn.commit()
        conn.close()
        servicios = fresh_db.get_servicios()
        assert all(s["id"] != 1 for s in servicios)


class TestProfesionales:
    def test_get_profesionales_retorna_lista(self, fresh_db):
        profesionales = fresh_db.get_profesionales()
        assert isinstance(profesionales, list)
        assert len(profesionales) >= 1

    def test_profesional_tiene_campos(self, fresh_db):
        prof = fresh_db.get_profesionales()[0]
        assert "nombre" in prof
        assert "especialidad" in prof


class TestClientes:
    def test_get_or_create_crea_nuevo(self, fresh_db):
        cliente = fresh_db.get_or_create_cliente("test", TEST_USER)
        assert cliente["canal"] == "test"
        assert cliente["canal_user_id"] == TEST_USER
        assert cliente["id"] is not None

    def test_get_or_create_reusa_existente(self, fresh_db):
        c1 = fresh_db.get_or_create_cliente("test", TEST_USER)
        c2 = fresh_db.get_or_create_cliente("test", TEST_USER)
        assert c1["id"] == c2["id"]

    def test_get_or_create_con_nombre(self, fresh_db):
        cliente = fresh_db.get_or_create_cliente("test", TEST_USER, "Juan Pérez")
        assert cliente["nombre"] == "Juan Pérez"

    def test_get_or_create_actualiza_nombre(self, fresh_db):
        fresh_db.get_or_create_cliente("test", TEST_USER)
        cliente = fresh_db.get_or_create_cliente("test", TEST_USER, "Juan Pérez")
        assert cliente["nombre"] == "Juan Pérez"

    def test_canal_user_id_unico(self, fresh_db):
        fresh_db.get_or_create_cliente("test", "user_a")
        fresh_db.get_or_create_cliente("test", "user_b")
        conn = fresh_db.get_db()
        rows = conn.execute("SELECT * FROM clientes").fetchall()
        conn.close()
        assert len(rows) == 2


class TestHorasDisponibles:
    def test_horas_en_dia_laboral(self, fresh_db):
        """Días laborales tienen horarios configurados."""
        profesionales = fresh_db.get_profesionales()
        if not profesionales:
            pytest.skip("No hay profesionales")
        fechas = fresh_db.get_fechas_disponibles(profesionales[0]["id"], 60, 14)
        assert len(fechas) > 0

    def test_slots_respetan_duracion(self, fresh_db):
        """Slots de 30 min generan más opciones que slots de 60 min."""
        profesionales = fresh_db.get_profesionales()
        if not profesionales:
            pytest.skip("No hay profesionales")

        fecha_test = date.today()
        for i in range(1, 14):
            d = date.today()
            from datetime import timedelta
            d = d + timedelta(days=i)
            horas_30 = fresh_db.get_horas_disponibles(profesionales[0]["id"], d, 30)
            horas_60 = fresh_db.get_horas_disponibles(profesionales[0]["id"], d, 60)
            if horas_30 and horas_60:
                assert len(horas_30) >= len(horas_60) or len(horas_60) == 0
                return
        pytest.skip("No se encontraron fechas con disponibilidad")


class TestCitas:
    def test_crear_cita_retorna_datos(self, fresh_db):
        cliente = fresh_db.get_or_create_cliente("test", TEST_USER)
        servicios = fresh_db.get_servicios()
        profesionales = fresh_db.get_profesionales()
        cita = fresh_db.crear_cita(
            cliente["id"], servicios[0]["id"], profesionales[0]["id"],
            "2026-06-10", "10:00"
        )
        assert cita["cliente_id"] == cliente["id"]
        assert cita["fecha"] == "2026-06-10"
        assert cita["hora"] == "10:00"
        assert cita["estado"] == "confirmada"

    def test_cancelar_cita(self, fresh_db):
        cliente = fresh_db.get_or_create_cliente("test", TEST_USER)
        servicios = fresh_db.get_servicios()
        profesionales = fresh_db.get_profesionales()
        cita = fresh_db.crear_cita(
            cliente["id"], servicios[0]["id"], profesionales[0]["id"],
            "2026-06-10", "10:00"
        )
        fresh_db.cancelar_cita(cita["id"])
        citas = fresh_db.get_citas_cliente(cliente["id"])
        assert len(citas) == 0

    def test_modificar_cita(self, fresh_db):
        from datetime import timedelta
        cliente = fresh_db.get_or_create_cliente("test", TEST_USER)
        servicios = fresh_db.get_servicios()
        profesionales = fresh_db.get_profesionales()
        # Dynamic future dates: get_citas_cliente filters fecha >= today,
        # so hardcoded dates would rot and empty the result list over time
        fecha_original = (date.today() + timedelta(days=7)).isoformat()
        fecha_nueva = (date.today() + timedelta(days=8)).isoformat()
        cita = fresh_db.crear_cita(
            cliente["id"], servicios[0]["id"], profesionales[0]["id"],
            fecha_original, "10:00"
        )
        fresh_db.modificar_cita(cita["id"], fecha_nueva, "11:00")
        citas = fresh_db.get_citas_cliente(cliente["id"])
        assert citas[0]["fecha"] == fecha_nueva
        assert citas[0]["hora"] == "11:00"

    def test_get_citas_solo_futuras(self, fresh_db):
        """get_citas_cliente solo retorna citas desde hoy en adelante."""
        cliente = fresh_db.get_or_create_cliente("test", TEST_USER)
        servicios = fresh_db.get_servicios()
        profesionales = fresh_db.get_profesionales()
        from datetime import timedelta
        ayer = (date.today() - timedelta(days=1)).isoformat()
        manana = (date.today() + timedelta(days=1)).isoformat()
        fresh_db.crear_cita(cliente["id"], servicios[0]["id"], profesionales[0]["id"], ayer, "10:00")
        fresh_db.crear_cita(cliente["id"], servicios[0]["id"], profesionales[0]["id"], manana, "10:00")
        citas = fresh_db.get_citas_cliente(cliente["id"])
        fechas = [c["fecha"] for c in citas]
        assert ayer not in fechas


class TestDisponibilidadFechas:
    def test_no_devuelve_fechas_pasadas(self, fresh_db):
        """get_fechas_disponibles no incluye hoy ni días pasados."""
        profesionales = fresh_db.get_profesionales()
        if not profesionales:
            pytest.skip("No hay profesionales")
        fechas = fresh_db.get_fechas_disponibles(profesionales[0]["id"], 60, 14)
        hoy = date.today()
        for f in fechas:
            assert f > hoy

    def test_ocupacion_bloquea_slot(self, fresh_db):
        """Una cita existente ocupa el slot."""
        profesionales = fresh_db.get_profesionales()
        servicios = fresh_db.get_servicios()
        if not profesionales or not servicios:
            pytest.skip("Faltan datos semilla")
        fechas = fresh_db.get_fechas_disponibles(profesionales[0]["id"], 60, 14)
        if not fechas:
            pytest.skip("No hay fechas disponibles")
        fecha = fechas[0]
        horas_antes = fresh_db.get_horas_disponibles(profesionales[0]["id"], fecha, 60)
        if not horas_antes:
            pytest.skip("No hay horas disponibles en la fecha")

        cliente = fresh_db.get_or_create_cliente("test", TEST_USER)
        fresh_db.crear_cita(cliente["id"], servicios[0]["id"], profesionales[0]["id"],
                            fecha.isoformat(), horas_antes[0])
        horas_despues = fresh_db.get_horas_disponibles(profesionales[0]["id"], fecha, 60)
        assert horas_antes[0] not in horas_despues
