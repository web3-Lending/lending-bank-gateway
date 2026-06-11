from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.context import IdentifierMiddleware
from app.core.s2s import S2SMiddleware


def _app(secret: str | None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(S2SMiddleware, secret=secret, exempt_paths={"/healthz", "/readyz"})
    app.add_middleware(IdentifierMiddleware)

    @app.get("/healthz")
    async def hz() -> dict:  # type: ignore[misc]
        return {"ok": True}

    @app.post("/api/v1/x")
    async def x() -> dict:  # type: ignore[misc]
        return {"ok": True}

    return app


def test_exempt_path_passes_without_headers() -> None:
    assert TestClient(_app("sec")).get("/healthz").status_code == 200


def test_missing_caller_service_rejected() -> None:
    r = TestClient(_app("sec")).post("/api/v1/x")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "GW_401_S2S"


def test_caller_with_valid_token_passes() -> None:
    r = TestClient(_app("sec")).post(
        "/api/v1/x", headers={"X-Caller-Service": "lifecycle", "X-S2S-Token": "sec"}
    )
    assert r.status_code == 200


def test_caller_with_bad_token_rejected() -> None:
    r = TestClient(_app("sec")).post(
        "/api/v1/x", headers={"X-Caller-Service": "lifecycle", "X-S2S-Token": "wrong"}
    )
    assert r.status_code == 401


def test_secret_unset_only_requires_caller_header() -> None:
    r = TestClient(_app(None)).post("/api/v1/x", headers={"X-Caller-Service": "lifecycle"})
    assert r.status_code == 200
