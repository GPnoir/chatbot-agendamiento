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


# ---------------------------------------------------------------------------
# Panel admin — vista Reporte (UI de métricas, issue #15)
# ---------------------------------------------------------------------------

class TestAdminPanelDashboard:
    """El panel admin sirve la vista de Reporte además de la Agenda.

    El reporte se renderiza client-side consumiendo /admin/reporte con la
    misma API key del login; el shell HTML no embebe datos ni secretos.
    """

    def test_panel_incluye_ambas_vistas(self, admin_client):
        resp = admin_client.get("/admin/panel")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        html = resp.text
        assert 'id="view-agenda"' in html
        assert 'id="view-reporte"' in html
        # La vista de reporte consume el endpoint de métricas existente.
        assert "/admin/reporte" in html

    def test_panel_reporte_usa_authorization_header(self, admin_client):
        html = admin_client.get("/admin/panel").text
        assert "?token=" not in html
        assert "location.search" not in html
        assert "Authorization" in html

    def test_panel_sin_paleta_material_ni_side_stripe(self, admin_client):
        """Regresión de diseño: fuera el verde Material y el side-stripe."""
        html = admin_client.get("/admin/panel").text
        assert "#4caf50" not in html
        assert "#2d5a27" not in html
        assert "border-left:3px solid" not in html

    def test_panel_sigue_siendo_login_shell(self, admin_client):
        """El rediseño mantiene el contrato de seguridad del shell."""
        html = admin_client.get("/admin/panel").text
        assert "login-overlay" in html
        assert "sessionStorage" in html
        assert 'style="--days:7;display:none"' in html

    def test_template_rutea_admin_reporte(self):
        """API Gateway debe enrutar GET /admin/reporte.

        El endpoint existe en FastAPI y pasa por TestClient, pero en prod solo
        son alcanzables los paths declarados como evento Api en template.yaml.
        Sin esta ruta, la vista Reporte recibiría 403 de API Gateway.
        """
        import pathlib
        template = pathlib.Path(__file__).resolve().parents[1] / "template.yaml"
        assert "Path: /admin/reporte" in template.read_text()

    def test_panel_login_usuario_password(self, admin_client):
        """El login del panel es usuario+contraseña contra POST /admin/login."""
        html = admin_client.get("/admin/panel").text
        assert 'id="login-user"' in html
        assert 'id="login-pass"' in html
        assert "/admin/login" in html

    def test_template_rutea_admin_login(self):
        """API Gateway debe enrutar POST /admin/login (si no, 403 en prod)."""
        import pathlib
        template = pathlib.Path(__file__).resolve().parents[1] / "template.yaml"
        assert "Path: /admin/login" in template.read_text()

    def test_panel_menu_hamburguesa(self, admin_client):
        """El panel trae menú hamburguesa con Agenda/Reporte/Fichas/Logout."""
        html = admin_client.get("/admin/panel").text
        assert 'id="nav-toggle"' in html        # botón hamburguesa
        assert 'id="nav-drawer"' in html        # drawer de navegación
        assert 'id="nav-agenda"' in html
        assert 'id="nav-reporte"' in html
        assert 'id="nav-fichas"' in html
        assert 'id="view-fichas"' in html       # vista Fichas (placeholder)
        assert "Cerrar sesión" in html          # logout
        # El control segmentado de la cabecera fue reemplazado por el menú.
        assert 'id="tab-agenda"' not in html

    def test_panel_detalle_de_cita(self, admin_client):
        """El panel trae el panel lateral de detalle con cancelar."""
        html = admin_client.get("/admin/panel").text
        assert 'id="detail-panel"' in html
        assert "openDetail" in html             # click en una cita abre el detalle
        assert "/admin/cita/cancelar" in html   # acción de cancelar

    def test_template_rutea_cancelar_cita(self):
        """API Gateway debe enrutar POST /admin/cita/cancelar (si no, 403 en prod)."""
        import pathlib
        template = pathlib.Path(__file__).resolve().parents[1] / "template.yaml"
        assert "Path: /admin/cita/cancelar" in template.read_text()

    def test_panel_fichas_funcional(self, admin_client):
        """La vista Fichas consume los endpoints de pacientes y notas."""
        html = admin_client.get("/admin/panel").text
        assert "loadFichas" in html            # carga la lista de pacientes
        assert "/admin/clientes" in html
        assert "/admin/cliente/nota" in html   # agregar nota
        assert "addNota" in html

    def test_template_rutea_fichas(self):
        """API Gateway debe enrutar los endpoints de fichas (si no, 403 en prod)."""
        import pathlib
        txt = (pathlib.Path(__file__).resolve().parents[1] / "template.yaml").read_text()
        assert "Path: /admin/clientes" in txt
        assert "Path: /admin/cliente\n" in txt
        assert "Path: /admin/cliente/nota" in txt
