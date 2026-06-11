import logging

import pytest
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


def test_empty_string_secret_treated_as_dev_mode() -> None:
    """空串 secret 与 None 等价：仅校验 caller 头，不校验 token，POST 带 caller 头应 200。"""
    r = TestClient(_app("")).post("/api/v1/x", headers={"X-Caller-Service": "lifecycle"})
    assert r.status_code == 200


def test_trailing_slash_exempt_path_not_blocked() -> None:
    """带尾斜杠的豁免路径（如 /healthz/）应放行到路由层，不被 S2S 拦截（非 401）。"""
    r = TestClient(_app("sec")).get("/healthz/")
    # S2S 不拦截（不是 401）；路由层可能 404/200/307，语义是"没被 S2S 拦"
    assert r.status_code != 401


def test_auth_failure_logs_warning_no_token_leak(caplog: pytest.LogCaptureFixture) -> None:
    """鉴权失败时产生 warning 日志，且不含 token 原值。"""
    with caplog.at_level(logging.WARNING, logger="app.core.s2s"):
        TestClient(_app("sec")).post(
            "/api/v1/x", headers={"X-Caller-Service": "lifecycle", "X-S2S-Token": "wrong"}
        )
    assert any("s2s auth failed" in r.message for r in caplog.records)
    # 确认 token 原值未泄漏到日志
    assert all("wrong" not in r.message for r in caplog.records)


def test_missing_caller_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """缺 caller 头时产生 warning 日志。"""
    with caplog.at_level(logging.WARNING, logger="app.core.s2s"):
        TestClient(_app("sec")).post("/api/v1/x")
    assert any("s2s auth failed" in r.message for r in caplog.records)
