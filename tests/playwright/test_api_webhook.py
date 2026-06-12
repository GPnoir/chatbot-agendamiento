"""Playwright API test: verificación de webhook de WhatsApp.

Meta (Facebook) verifica la propiedad del webhook enviando un GET
con los parámetros hub.mode, hub.verify_token y hub.challenge.
"""
import json

from playwright.sync_api import APIRequestContext

from tests.playwright.conftest import E2E_WHATSAPP_VERIFY_TOKEN as VERIFY_TOKEN, sign_payload


def test_verificacion_exitosa(api_context: APIRequestContext):
    resp = api_context.get(
        "/whatsapp/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "challenge_abc_123",
        },
    )
    assert resp.status == 200
    assert resp.text() == "challenge_abc_123"


def test_verificacion_token_invalido(api_context: APIRequestContext):
    resp = api_context.get(
        "/whatsapp/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "token_equivocado",
            "hub.challenge": "challenge_456",
        },
    )
    assert resp.status == 403


def test_verificacion_sin_mode(api_context: APIRequestContext):
    resp = api_context.get(
        "/whatsapp/webhook",
        params={
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "challenge_789",
        },
    )
    assert resp.status == 403


def test_verificacion_mode_invalido(api_context: APIRequestContext):
    resp = api_context.get(
        "/whatsapp/webhook",
        params={
            "hub.mode": "unsubscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "challenge_012",
        },
    )
    assert resp.status == 403


def test_post_sin_firma_retorna_403(api_context: APIRequestContext):
    """Unsigned POST must be rejected — regression for fail-closed security."""
    body = json.dumps({"entry": []}).encode()
    resp = api_context.post(
        "/whatsapp/webhook",
        data=body,
        headers={"Content-Type": "application/json"},
        fail_on_status_code=False,
    )
    assert resp.status == 403


def test_post_firma_invalida_retorna_403(api_context: APIRequestContext):
    """Badly-signed POST must be rejected."""
    body = json.dumps({"entry": []}).encode()
    resp = api_context.post(
        "/whatsapp/webhook",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=deadbeefdeadbeef",
        },
        fail_on_status_code=False,
    )
    assert resp.status == 403
