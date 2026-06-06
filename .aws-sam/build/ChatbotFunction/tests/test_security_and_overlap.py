"""Tests para rate limiter, input sanitization y solapamiento de horarios."""
import time
from datetime import date, timedelta

import pytest

import chatbot
import database as db_module
import rate_limiter
from config import MENSAJES
from tests.conftest import TEST_USER


class TestRateLimiter:
    def test_permite_hasta_limite(self):
        """20 mensajes seguidos no son bloqueados."""
        for i in range(20):
            assert not rate_limiter.is_rate_limited(f"rate_user_{i % 5}")

    def test_bloquea_sobre_limite(self):
        """El mensaje 21 del mismo usuario es bloqueado."""
        user = "rate_heavy_user"
        for _ in range(20):
            rate_limiter.is_rate_limited(user)
        assert rate_limiter.is_rate_limited(user)

    def test_usuarios_independientes(self):
        """Rate limit de un usuario no afecta a otro."""
        for _ in range(20):
            rate_limiter.is_rate_limited("user_a_rate")
        assert rate_limiter.is_rate_limited("user_a_rate")
        assert not rate_limiter.is_rate_limited("user_b_rate")

    def test_reset_limpia_estado(self):
        """reset() permite mensajes de nuevo."""
        user = "rate_reset_user"
        for _ in range(20):
            rate_limiter.is_rate_limited(user)
        assert rate_limiter.is_rate_limited(user)
        rate_limiter.reset()
        assert not rate_limiter.is_rate_limited(user)

    def test_chatbot_responde_rate_limit(self, fresh_db):
        """Chatbot retorna mensaje de rate limit al exceder."""
        for _ in range(20):
            chatbot.handle_message("test", TEST_USER, "menu")
        resp = chatbot.handle_message("test", TEST_USER, "menu")
        assert "rápido" in resp or "⚠️" in resp


class TestInputSanitization:
    def test_mensaje_largo_truncado(self, fresh_db):
        """Mensajes de más de 500 chars se truncan sin error."""
        texto_largo = "a" * 1000
        resp = chatbot.handle_message("test", TEST_USER, texto_largo)
        # No debe crashear, debe retornar bienvenida (texto no es comando válido)
        assert resp is not None

    def test_null_bytes_removidos(self, fresh_db):
        """Null bytes en el mensaje no causan problemas."""
        resp = chatbot.handle_message("test", TEST_USER, "menu\x00\x00")
        assert "Hola" in resp or "🌸" in resp

    def test_espacios_se_limpian(self, fresh_db):
        """Espacios al inicio/fin se limpian."""
        resp = chatbot.handle_message("test", TEST_USER, "  menu  ")
        assert "Hola" in resp or "🌸" in resp


class TestSolapamientoHorarios:
    """Verifica que citas de distintas duraciones no se solapan."""

    def test_cita_60min_bloquea_slots_intermedios(self, fresh_db):
        """Una cita de 60min a las 10:00 bloquea 10:00 y 10:30."""
        profesionales = db_module.get_profesionales()
        servicios = db_module.get_servicios()
        if not profesionales or not servicios:
            pytest.skip("Sin datos semilla")

        prof_id = profesionales[0]["id"]
        # Buscar fecha con disponibilidad
        fechas = db_module.get_fechas_disponibles(prof_id, 60, 14)
        if not fechas:
            pytest.skip("Sin fechas disponibles")
        fecha = fechas[0]

        # Obtener horas antes de agendar
        horas_antes = db_module.get_horas_disponibles(prof_id, fecha, 30)
        if "10:00" not in horas_antes:
            pytest.skip("10:00 no disponible")

        # Crear cita de 60 min a las 10:00
        cliente = db_module.get_or_create_cliente("test", "overlap_user")
        serv_60 = next(s for s in servicios if s["duracion_min"] == 60)
        db_module.crear_cita(cliente["id"], serv_60["id"], prof_id, fecha.isoformat(), "10:00")

        # Verificar: 10:00 y 10:30 no deben estar disponibles para servicio de 30 min
        horas_despues = db_module.get_horas_disponibles(prof_id, fecha, 30)
        assert "10:00" not in horas_despues
        assert "10:30" not in horas_despues

    def test_cita_30min_no_bloquea_siguiente_hora(self, fresh_db):
        """Una cita de 30min a las 10:00 NO bloquea las 10:30."""
        profesionales = db_module.get_profesionales()
        servicios = db_module.get_servicios()
        if not profesionales or not servicios:
            pytest.skip("Sin datos semilla")

        prof_id = profesionales[0]["id"]
        fechas = db_module.get_fechas_disponibles(prof_id, 30, 14)
        if not fechas:
            pytest.skip("Sin fechas disponibles")
        fecha = fechas[0]

        horas_antes = db_module.get_horas_disponibles(prof_id, fecha, 30)
        if "10:00" not in horas_antes or "10:30" not in horas_antes:
            pytest.skip("10:00/10:30 no disponibles")

        # Crear cita de 30 min a las 10:00
        cliente = db_module.get_or_create_cliente("test", "overlap_user_2")
        serv_30 = next(s for s in servicios if s["duracion_min"] == 30)
        db_module.crear_cita(cliente["id"], serv_30["id"], prof_id, fecha.isoformat(), "10:00")

        # 10:30 debe seguir disponible
        horas_despues = db_module.get_horas_disponibles(prof_id, fecha, 30)
        assert "10:00" not in horas_despues
        assert "10:30" in horas_despues

    def test_servicio_largo_no_cabe_antes_de_cita(self, fresh_db):
        """Un servicio de 60min no cabe a las 10:00 si hay cita a las 10:30."""
        profesionales = db_module.get_profesionales()
        servicios = db_module.get_servicios()
        if not profesionales or not servicios:
            pytest.skip("Sin datos semilla")

        prof_id = profesionales[0]["id"]
        fechas = db_module.get_fechas_disponibles(prof_id, 30, 14)
        if not fechas:
            pytest.skip("Sin fechas disponibles")
        fecha = fechas[0]

        horas_antes = db_module.get_horas_disponibles(prof_id, fecha, 60)
        if "10:00" not in horas_antes:
            pytest.skip("10:00 no disponible para 60min")

        # Crear cita de 30 min a las 10:30
        cliente = db_module.get_or_create_cliente("test", "overlap_user_3")
        serv_30 = next(s for s in servicios if s["duracion_min"] == 30)
        db_module.crear_cita(cliente["id"], serv_30["id"], prof_id, fecha.isoformat(), "10:30")

        # 10:00 no debe estar disponible para servicio de 60 min (colisiona con 10:30)
        horas_despues = db_module.get_horas_disponibles(prof_id, fecha, 60)
        assert "10:00" not in horas_despues

    def test_multiples_citas_distintas_duraciones(self, fresh_db):
        """Múltiples citas de distintas duraciones se respetan mutuamente."""
        profesionales = db_module.get_profesionales()
        servicios = db_module.get_servicios()
        if not profesionales or not servicios:
            pytest.skip("Sin datos semilla")

        prof_id = profesionales[0]["id"]
        fechas = db_module.get_fechas_disponibles(prof_id, 30, 14)
        if not fechas:
            pytest.skip("Sin fechas disponibles")
        fecha = fechas[0]

        cliente = db_module.get_or_create_cliente("test", "overlap_user_4")
        serv_60 = next(s for s in servicios if s["duracion_min"] == 60)
        serv_30 = next(s for s in servicios if s["duracion_min"] == 30)

        # Cita 60min a las 09:00, cita 30min a las 11:00
        db_module.crear_cita(cliente["id"], serv_60["id"], prof_id, fecha.isoformat(), "09:00")
        db_module.crear_cita(cliente["id"], serv_30["id"], prof_id, fecha.isoformat(), "11:00")

        horas = db_module.get_horas_disponibles(prof_id, fecha, 30)
        # 09:00 y 09:30 bloqueadas por cita de 60min
        assert "09:00" not in horas
        assert "09:30" not in horas
        # 11:00 bloqueada por cita de 30min
        assert "11:00" not in horas
        # 10:00, 10:30, 11:30 deben estar libres
        assert "10:00" in horas
        assert "10:30" in horas
        assert "11:30" in horas
