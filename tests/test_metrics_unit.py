"""Tests para métricas y reportes (issue #15).

Cubre la agregación en database_dynamo (resumen_citas_rango), el comando
admin /reporte vía chatbot_lambda y el endpoint JSON /admin/reporte.
"""
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import chatbot_lambda
import database_dynamo as db

ADMIN_ID = "admin_test_999"
VALID_KEY = "test-admin-key-secure-12345"


def _fecha(dias_atras: int) -> str:
    return (date.today() - timedelta(days=dias_atras)).isoformat()


def _seed_citas():
    """Crea citas de prueba: 3 dentro de la última semana, 1 fuera.

    Dentro del rango: 2 confirmadas (servicios 1 y 2) + 1 cancelada (servicio 1).
    Fuera del rango (hace 20 días): 1 confirmada.
    """
    cliente = db.get_or_create_cliente("telegram", "metrics_user", "Rita")
    db.crear_cita(cliente["id"], 1, 1, _fecha(1), "10:00")
    db.crear_cita(cliente["id"], 2, 1, _fecha(3), "11:00")
    cancelada = db.crear_cita(cliente["id"], 1, 1, _fecha(2), "12:00")
    db.cancelar_cita(cancelada["PK"], cancelada["SK"])
    db.crear_cita(cliente["id"], 1, 1, _fecha(20), "09:00")
    return cliente


# ---------------------------------------------------------------------------
# database_dynamo.resumen_citas_rango
# ---------------------------------------------------------------------------

class TestResumenCitasRango:
    def test_cuenta_total_y_estados(self):
        _seed_citas()
        resumen = db.resumen_citas_rango(_fecha(6), _fecha(0))
        assert resumen["total"] == 3
        assert resumen["por_estado"]["confirmada"] == 2
        assert resumen["por_estado"]["cancelada"] == 1

    def test_excluye_citas_fuera_de_rango(self):
        _seed_citas()
        resumen = db.resumen_citas_rango(_fecha(6), _fecha(0))
        # la cita de hace 20 días no cuenta
        assert resumen["total"] == 3

    def test_rango_amplio_incluye_todo(self):
        _seed_citas()
        resumen = db.resumen_citas_rango(_fecha(30), _fecha(0))
        assert resumen["total"] == 4

    def test_desglose_por_servicio(self):
        _seed_citas()
        resumen = db.resumen_citas_rango(_fecha(6), _fecha(0))
        assert resumen["por_servicio"]["Consulta inicial"] == 2
        assert resumen["por_servicio"]["Sesión de seguimiento"] == 1

    def test_tasa_cancelacion(self):
        _seed_citas()
        resumen = db.resumen_citas_rango(_fecha(6), _fecha(0))
        assert resumen["tasa_cancelacion"] == pytest.approx(1 / 3)

    def test_sin_citas_retorna_ceros(self):
        resumen = db.resumen_citas_rango(_fecha(6), _fecha(0))
        assert resumen["total"] == 0
        assert resumen["tasa_cancelacion"] == 0.0
        assert resumen["por_estado"] == {}
        assert resumen["por_servicio"] == {}


# ---------------------------------------------------------------------------
# Comando admin /reporte
# ---------------------------------------------------------------------------

class TestComandoReporte:
    def test_reporte_semana_por_defecto(self):
        _seed_citas()
        with patch("config.ADMIN_USER_ID", ADMIN_ID):
            resp = chatbot_lambda.handle_message("telegram", ADMIN_ID, "/reporte")
        assert "📊" in resp
        assert "Total citas: 3" in resp
        assert "Confirmadas: 2" in resp
        assert "Canceladas: 1" in resp
        assert "Consulta inicial: 2" in resp

    def test_reporte_mes_incluye_mas_citas(self):
        _seed_citas()
        with patch("config.ADMIN_USER_ID", ADMIN_ID):
            resp = chatbot_lambda.handle_message("telegram", ADMIN_ID, "/reporte mes")
        assert "Total citas: 4" in resp

    def test_reporte_sin_citas(self):
        with patch("config.ADMIN_USER_ID", ADMIN_ID):
            resp = chatbot_lambda.handle_message("telegram", ADMIN_ID, "/reporte")
        assert "Total citas: 0" in resp

    def test_usuario_no_admin_no_accede_al_reporte(self):
        _seed_citas()
        resp = chatbot_lambda.handle_message("telegram", "usuario_comun", "/reporte")
        assert "Total citas" not in resp

    def test_ayuda_menciona_reporte(self):
        with patch("config.ADMIN_USER_ID", ADMIN_ID):
            resp = chatbot_lambda.handle_message("telegram", ADMIN_ID, "/ayuda")
        assert "/reporte" in resp


# ---------------------------------------------------------------------------
# Endpoint /admin/reporte
# ---------------------------------------------------------------------------

@pytest.fixture()
def admin_client():
    import config
    import lambda_handler

    with patch.object(config, "ADMIN_API_KEY", VALID_KEY), \
         patch("lambda_handler.ADMIN_API_KEY", VALID_KEY):
        with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
            yield c


class TestAdminReporteEndpoint:
    def test_sin_auth_rechazado(self, admin_client):
        resp = admin_client.get("/admin/reporte")
        assert resp.status_code == 401

    def test_key_invalida_rechazada(self, admin_client):
        resp = admin_client.get(
            "/admin/reporte", headers={"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401

    def test_reporte_default_ultima_semana(self, admin_client):
        _seed_citas()
        resp = admin_client.get(
            "/admin/reporte", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["por_estado"]["confirmada"] == 2
        assert data["desde"] == _fecha(6)
        assert data["hasta"] == _fecha(0)

    def test_reporte_rango_explicito(self, admin_client):
        _seed_citas()
        resp = admin_client.get(
            f"/admin/reporte?desde={_fecha(30)}&hasta={_fecha(0)}",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.json()["total"] == 4

    def test_fechas_invalidas_retornan_400(self, admin_client):
        resp = admin_client.get(
            "/admin/reporte?desde=garbage&hasta=2026-06-12",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 400
