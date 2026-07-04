"""Tests de guardrails de infraestructura en template.yaml.

Un flood de requests (aunque traigan firma inválida) invoca la Lambda una vez
por request; sin límites, el costo no tiene techo. Estos tests fijan el
contrato de template.yaml:

1. Throttling en API Gateway (rate + burst) para todo el stage.
2. ReservedConcurrentExecutions en cada Lambda (techo de costo/concurrencia).
3. Alarma de billing (EstimatedCharges) con umbral parametrizado — el aviso
   temprano si algo se escapa de los límites anteriores.

Parsean template.yaml directamente: si alguien borra un guardrail, el test
rompe antes del deploy.
"""
from pathlib import Path

import yaml

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "template.yaml"


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader que tolera los tags de CloudFormation (!Ref, !Sub, !If...)."""


def _cfn_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {tag_suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag_suffix: loader.construct_sequence(node)}
    return {tag_suffix: loader.construct_mapping(node)}


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def _load_template():
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        return yaml.load(f, Loader=_CfnLoader)


class TestApiGatewayThrottling:
    """El stage entero debe tener throttling: acota floods antes de la Lambda."""

    def test_globals_api_define_method_settings(self):
        template = _load_template()
        api_globals = template.get("Globals", {}).get("Api", {})
        assert "MethodSettings" in api_globals, (
            "Globals.Api.MethodSettings falta: sin throttling, un flood "
            "invoca la Lambda una vez por request sin techo de costo"
        )

    def test_throttling_cubre_todas_las_rutas(self):
        template = _load_template()
        settings = template["Globals"]["Api"]["MethodSettings"]
        wildcard = [
            s for s in settings
            if s.get("ResourcePath") == "/*" and s.get("HttpMethod") == "*"
        ]
        assert wildcard, "El throttling debe aplicar a todas las rutas (/* y método *)"

    def test_rate_y_burst_son_positivos_y_acotados(self):
        template = _load_template()
        settings = template["Globals"]["Api"]["MethodSettings"]
        wildcard = next(
            s for s in settings
            if s.get("ResourcePath") == "/*" and s.get("HttpMethod") == "*"
        )
        rate = wildcard.get("ThrottlingRateLimit")
        burst = wildcard.get("ThrottlingBurstLimit")
        assert isinstance(rate, (int, float)) and rate > 0
        assert isinstance(burst, int) and burst > 0
        # Techo sano: este bot atiende webhooks de 2 canales + un panel de una
        # sola terapeuta. Si alguien "afloja" el límite por sobre esto, que lo
        # justifique tocando también el test.
        assert rate <= 50, "rate limit demasiado alto para el tráfico real del bot"
        assert burst <= 100, "burst limit demasiado alto para el tráfico real del bot"


class TestReservedConcurrency:
    """Cada Lambda con techo de concurrencia: acota el costo máximo de un flood."""

    def _functions(self, template):
        return {
            name: res
            for name, res in template["Resources"].items()
            if res.get("Type") == "AWS::Serverless::Function"
        }

    def test_toda_lambda_tiene_reserved_concurrency(self):
        template = _load_template()
        sin_limite = [
            name
            for name, res in self._functions(template).items()
            if "ReservedConcurrentExecutions" not in res.get("Properties", {})
        ]
        assert not sin_limite, (
            f"Lambdas sin ReservedConcurrentExecutions (costo sin techo): {sin_limite}"
        )

    def test_chatbot_function_techo_sano(self):
        template = _load_template()
        rce = template["Resources"]["ChatbotFunction"]["Properties"][
            "ReservedConcurrentExecutions"
        ]
        assert isinstance(rce, int) and 1 <= rce <= 50

    def test_lambdas_programadas_concurrencia_uno(self):
        """Reminder y AttendancePrompt corren 1 vez/hora: no necesitan más de 1,
        y con 1 se evita además que dos ejecuciones se solapen."""
        template = _load_template()
        for name in ("ReminderFunction", "AttendancePromptFunction"):
            rce = template["Resources"][name]["Properties"][
                "ReservedConcurrentExecutions"
            ]
            assert rce == 1, f"{name} debería tener ReservedConcurrentExecutions: 1"


class TestBillingAlarm:
    """Alarma de gasto estimado de la cuenta: el aviso de última línea."""

    def test_parametro_presupuesto_mensual(self):
        template = _load_template()
        params = template["Parameters"]
        assert "MonthlyBudgetUsd" in params, "Falta el parámetro MonthlyBudgetUsd"
        default = params["MonthlyBudgetUsd"].get("Default")
        assert float(default) > 0

    def test_alarma_billing_existe_y_usa_estimated_charges(self):
        template = _load_template()
        alarm = template["Resources"].get("AlarmBillingEstimatedCharges")
        assert alarm is not None, "Falta la alarma AlarmBillingEstimatedCharges"
        props = alarm["Properties"]
        assert props["Namespace"] == "AWS/Billing"
        assert props["MetricName"] == "EstimatedCharges"
        dims = {d["Name"]: d["Value"] for d in props["Dimensions"]}
        assert dims.get("Currency") == "USD"
        # Maximum: EstimatedCharges es un acumulado mensual, el máximo del
        # período es el valor real más reciente.
        assert props["Statistic"] == "Maximum"

    def test_alarma_billing_umbral_parametrizado(self):
        template = _load_template()
        props = template["Resources"]["AlarmBillingEstimatedCharges"]["Properties"]
        # El umbral debe salir del parámetro, no estar hardcodeado.
        assert props["Threshold"] == {"Ref": "MonthlyBudgetUsd"}

    def test_alarma_billing_notifica_por_el_topic_existente(self):
        template = _load_template()
        props = template["Resources"]["AlarmBillingEstimatedCharges"]["Properties"]
        # Mismo patrón que el resto de las alarmas: acciones condicionales
        # al AlarmTopic solo cuando hay AlarmEmail configurado.
        assert props["AlarmActions"] == {
            "If": ["HasAlarmEmail", [{"Ref": "AlarmTopic"}], []]
        }
