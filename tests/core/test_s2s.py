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


# ── caller 白名单测试 ──────────────────────────────────────────────────────────


def _app_with_callers(secret: str | None, callers: set[str] | None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        S2SMiddleware,
        secret=secret,
        exempt_paths={"/healthz"},
        allowed_callers=callers,
    )
    app.add_middleware(IdentifierMiddleware)

    @app.post("/api/v1/x")
    async def x() -> dict:  # type: ignore[misc]
        return {"ok": True}

    return app


def test_caller_whitelist_unknown_caller_rejected() -> None:
    """白名单启用时，caller 不在白名单 → 401 GW_401_S2S。"""
    r = TestClient(_app_with_callers("sec", {"lending-lifecycel"})).post(
        "/api/v1/x",
        headers={"X-Caller-Service": "unknown-svc", "X-S2S-Token": "sec"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "GW_401_S2S"


def test_caller_whitelist_known_caller_passes() -> None:
    """白名单启用时，caller 在白名单内 → 200。"""
    r = TestClient(_app_with_callers("sec", {"lending-lifecycel", "lending-risk"})).post(
        "/api/v1/x",
        headers={"X-Caller-Service": "lending-lifecycel", "X-S2S-Token": "sec"},
    )
    assert r.status_code == 200


def test_caller_whitelist_none_disables_check() -> None:
    """白名单为 None（空=不启用）时，任意 caller 通过 token 校验即放行。"""
    r = TestClient(_app_with_callers("sec", None)).post(
        "/api/v1/x",
        headers={"X-Caller-Service": "any-service", "X-S2S-Token": "sec"},
    )
    assert r.status_code == 200


# ── per-service token（A-m-002）────────────────────────────────────────────────


def _app_per_service(caller_tokens: dict[str, str]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        S2SMiddleware,
        secret="shared-secret-ignored",  # noqa: S106  # 测试用；per-service 模式下被忽略
        exempt_paths={"/healthz"},
        caller_tokens=caller_tokens,
    )
    app.add_middleware(IdentifierMiddleware)

    @app.post("/api/v1/x")
    async def x() -> dict:  # type: ignore[misc]
        return {"ok": True}

    return app


def test_per_service_correct_caller_and_token_passes() -> None:
    """per-service：caller 用自己的专属 token → 放行。"""
    c = TestClient(_app_per_service({"svc-a": "tok-a", "svc-b": "tok-b"}))
    r = c.post("/api/v1/x", headers={"X-Caller-Service": "svc-a", "X-S2S-Token": "tok-a"})
    assert r.status_code == 200


def test_per_service_wrong_token_rejected() -> None:
    """per-service：caller 正确但 token 错 → 401。"""
    c = TestClient(_app_per_service({"svc-a": "tok-a"}))
    r = c.post("/api/v1/x", headers={"X-Caller-Service": "svc-a", "X-S2S-Token": "WRONG"})
    assert r.status_code == 401


def test_per_service_caller_using_others_token_rejected() -> None:
    """per-service 核心：caller A 拿 caller B 的 token → 401（caller↔token 密码学绑定）。"""
    c = TestClient(_app_per_service({"svc-a": "tok-a", "svc-b": "tok-b"}))
    r = c.post("/api/v1/x", headers={"X-Caller-Service": "svc-a", "X-S2S-Token": "tok-b"})
    assert r.status_code == 401


def test_per_service_unknown_caller_rejected() -> None:
    """per-service：未登记的 caller → 401（即使 token 是别人的）。"""
    c = TestClient(_app_per_service({"svc-a": "tok-a"}))
    r = c.post("/api/v1/x", headers={"X-Caller-Service": "ghost", "X-S2S-Token": "tok-a"})
    assert r.status_code == 401


def test_per_service_does_not_leak_token_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """per-service 校验失败日志不含 token 明文。"""
    c = TestClient(_app_per_service({"svc-a": "tok-a"}))
    with caplog.at_level(logging.WARNING):
        c.post("/api/v1/x", headers={"X-Caller-Service": "svc-a", "X-S2S-Token": "super-secret"})
    assert "super-secret" not in caplog.text
    assert any("bad_per_service_token" in r.message for r in caplog.records)


# ── API-HTTP-004：401 必须带 WWW-Authenticate（5 个 401 出口逐个钉住）────────────


def _app_callback(api_key: str | None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        S2SMiddleware,
        secret="sec",  # noqa: S106  # 测试用
        exempt_paths={"/healthz"},
        callback_paths={"/api/v1/callbacks/wedap/transactions"},
        callback_api_key=api_key,
    )
    app.add_middleware(IdentifierMiddleware)

    @app.post("/api/v1/callbacks/wedap/transactions")
    async def cb() -> dict:  # type: ignore[misc]
        return {"ok": True}

    return app


def test_401_missing_caller_has_www_authenticate() -> None:
    """出口 1/5：缺 X-Caller-Service。"""
    r = TestClient(_app("sec")).post("/api/v1/x")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("S2S ")
    assert 'realm="lending-bank-gateway"' in r.headers["www-authenticate"]


def test_401_bad_shared_secret_has_www_authenticate() -> None:
    """出口 2/5：共享 secret 不匹配。"""
    r = TestClient(_app("sec")).post(
        "/api/v1/x", headers={"X-Caller-Service": "lifecycle", "X-S2S-Token": "wrong"}
    )
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("S2S ")


def test_401_unknown_caller_has_www_authenticate() -> None:
    """出口 3/5：白名单外的 caller。"""
    r = TestClient(_app_with_callers("sec", {"lending-lifecycel"})).post(
        "/api/v1/x", headers={"X-Caller-Service": "unknown-svc", "X-S2S-Token": "sec"}
    )
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("S2S ")


def test_401_bad_per_service_token_has_www_authenticate() -> None:
    """出口 4/5：per-service 专属 token 不匹配。"""
    r = TestClient(_app_per_service({"svc-a": "tok-a"})).post(
        "/api/v1/x", headers={"X-Caller-Service": "svc-a", "X-S2S-Token": "WRONG"}
    )
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("S2S ")


def test_401_callback_bad_apikey_has_apikey_challenge() -> None:
    """出口 5/5：wedap 入站回调 apikey 失败 → challenge 必须指向 apikey 而不是 S2S。

    challenge 说错了等于告诉外部 wedap 去刷新一个它根本没有的凭证。
    """
    r = TestClient(_app_callback("right-key")).post(
        "/api/v1/callbacks/wedap/transactions", headers={"apikey": "wrong-key"}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "GW_401_CALLBACK"
    assert r.headers["www-authenticate"].startswith("ApiKey ")


def test_callback_valid_apikey_passes_without_challenge() -> None:
    """对照：apikey 正确时放行，且不带 WWW-Authenticate。"""
    r = TestClient(_app_callback("right-key")).post(
        "/api/v1/callbacks/wedap/transactions", headers={"apikey": "right-key"}
    )
    assert r.status_code == 200
    assert "www-authenticate" not in r.headers


# ── API-HTTP-013：401 响应体 trace_id 必须与响应头同源 ─────────────────────────


def test_401_trace_id_matches_response_header_when_caller_sends_none() -> None:
    """调用方没带 X-Trace-Id 时，401 body 的 trace_id 不得再是固定字面量 "trc-s2s"。

    此前所有这类 401 共用同一个假 trace_id，报障时无法定位到具体某次请求，
    而日志里记的又是另一个值 —— API-HTTP-013 的关联性直接失效。
    """
    r = TestClient(_app("sec")).post("/api/v1/x")
    assert r.status_code == 401
    body_trace = r.json()["trace_id"]
    assert body_trace == r.headers["x-trace-id"]
    assert body_trace != "trc-s2s"
    assert body_trace.startswith("trc-")


def test_401_trace_id_unique_per_request() -> None:
    """两次同样的匿名 401 必须拿到两个不同 trace_id（旧实现两次都是 "trc-s2s"）。"""
    client = TestClient(_app("sec"))
    first = client.post("/api/v1/x").json()["trace_id"]
    second = client.post("/api/v1/x").json()["trace_id"]
    assert first != second


def test_401_trace_id_reuses_caller_supplied_value() -> None:
    """调用方带了合规 X-Trace-Id 时仍然透传原值（链路不能被重签打断）。"""
    r = TestClient(_app("sec")).post("/api/v1/x", headers={"X-Trace-Id": "trc-caller-001"})
    assert r.json()["trace_id"] == "trc-caller-001"


def test_401_hostile_trace_id_resigned_in_body() -> None:
    """注入串不得经 401 响应体回显（s2s 也必须用过校验的 contextvar 值）。"""
    r = TestClient(_app("sec")).post(
        "/api/v1/x", headers={"X-Trace-Id": 'evil<script>"; DROP--'}
    )
    assert "script" not in r.text
    assert r.json()["trace_id"] == r.headers["x-trace-id"]
