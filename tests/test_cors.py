"""CORS is opt-in: no Access-Control-Allow-Origin header unless CORS_ORIGINS lists
the origin. Tested on fresh apps, so the shared app and its metrics are untouched."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import main

ORIGIN = "http://localhost:8001"
PREFLIGHT = {"Origin": ORIGIN, "Access-Control-Request-Method": "POST"}


def app_with(origins):
    app = FastAPI()

    @app.post("/risk/predeal")
    def predeal():
        return {}
    return app, main.add_cors(app, origins)


def test_cors_is_off_by_default():
    for unset in (None, "", " , "):
        app, enabled = app_with(unset)
        assert not enabled
        assert "access-control-allow-origin" not in TestClient(app).options("/risk/predeal", headers=PREFLIGHT).headers


def test_configured_origin_gets_cors_headers():
    app, enabled = app_with(f" {ORIGIN} , ,http://127.0.0.1:8001 ")
    assert enabled
    r = TestClient(app).options("/risk/predeal", headers=PREFLIGHT)
    assert r.headers.get("access-control-allow-origin") == ORIGIN
    other = TestClient(app).options("/risk/predeal", headers={**PREFLIGHT, "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_the_shared_app_has_no_cors_in_tests():
    assert not any("CORS" in str(m.cls) for m in main.app.user_middleware)
