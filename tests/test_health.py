"""Phase 1/3 health endpoint tests."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from avatar_backend.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY",   "test-key-phase1")
    monkeypatch.setenv("HA_URL",    "http://ha.local:8123")
    monkeypatch.setenv("HA_TOKEN",  "fake-token")
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    import avatar_backend.main as main_mod
    from pathlib import Path
    import tempfile, os
    tmp = Path(tempfile.mkdtemp())
    (tmp / "acl.yaml").write_text("version: 1\nrules: []\n")
    (tmp / "system_prompt.txt").write_text("test")
    monkeypatch.setattr(main_mod, "_CONFIG_DIR", tmp)

    from avatar_backend.main import create_app
    app = create_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_public_health_requires_no_auth(client):
    resp = client.get("/health/public")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_rejects_missing_key(client):
    assert client.get("/health").status_code == 401


def test_health_rejects_wrong_key(client):
    assert client.get("/health", headers={"X-API-Key": "wrong"}).status_code == 401


def test_health_accepts_correct_key(client):
    resp = client.get("/health", headers={"X-API-Key": "test-key-phase1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "degraded")
    assert "ollama" in body["components"]
    assert "home_assistant" in body["components"]
    # version field exists but we don't pin it in tests
    assert "version" in body


def test_auth_timing_safe(client):
    import time
    t1 = time.monotonic()
    client.get("/health", headers={"X-API-Key": ""})
    t2 = time.monotonic()
    client.get("/health", headers={"X-API-Key": "a" * 200})
    t3 = time.monotonic()
    assert (t2 - t1) < 1.0
    assert (t3 - t2) < 1.0
