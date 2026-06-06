"""Playwright API test: endpoint /health."""
from playwright.sync_api import APIRequestContext


def test_health_returns_ok(api_context: APIRequestContext):
    resp = api_context.get("/health")
    assert resp.status == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "chatbot-agendamiento"


def test_health_response_time(api_context: APIRequestContext):
    import time
    start = time.time()
    resp = api_context.get("/health")
    elapsed = time.time() - start
    assert resp.status == 200
    assert elapsed < 2.0, f"Response too slow: {elapsed:.2f}s"


def test_health_cors(api_context: APIRequestContext):
    resp = api_context.get(
        "/health",
        headers={"Origin": "http://localhost:3000"},
    )
    assert resp.status == 200
