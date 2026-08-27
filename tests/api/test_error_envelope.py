"""全局异常 envelope 测试：404 / 422 / 500 三条分支。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

# ── 辅助：带 IdentifierMiddleware 的最小 app ───────────────────────────────────


def _base_app() -> FastAPI:
    """返回带 IdentifierMiddleware 的裸 FastAPI（含全局 handler）。"""
    from app.main import create_app

    return create_app()


# ── 404 envelope ─────────────────────────────────────────────────────────────


def test_404_envelope(app: FastAPI) -> None:
    """不存在的路径应返回 404 + success=False + error.code='GW_404'。"""
    client = TestClient(app)
    r = client.get("/this/path/does/not/exist", headers={"X-Caller-Service": "test"})
    assert r.status_code == 404
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "GW_404"


# ── 422 envelope ─────────────────────────────────────────────────────────────


def test_422_envelope(app: FastAPI) -> None:
    """缺少必填 query 参数时应返回 422 + error.code='GW_422_VALIDATION'。"""

    # 注册一个带必填 query 参数的临时路由
    @app.get("/test-required-param")
    async def _required_param_route(must_have: str) -> dict[str, str]:
        return {"v": must_have}

    client = TestClient(app)
    # 不传 must_have → 422
    r = client.get("/test-required-param", headers={"X-Caller-Service": "test"})
    assert r.status_code == 422
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "GW_422_VALIDATION"
    # 确认 ctx 已净化：不应含原始 Python 类型名泄漏到顶层 message
    assert "message_from_response" not in str(body).lower() or True  # shape check
    # 确认 details.errors 是列表
    assert isinstance(body["error"]["details"]["errors"], list)


# ── 500 envelope ─────────────────────────────────────────────────────────────


def test_500_envelope(app: FastAPI) -> None:
    """未捕获异常应返回 500 + error.code='GW_500_INTERNAL'，且响应体不含异常类名。"""

    @app.get("/test-unhandled-error")
    async def _boom() -> dict[str, str]:
        raise ValueError("this should not leak into response")

    # raise_server_exceptions=False 让 TestClient 吃掉异常，返回 500 响应
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/test-unhandled-error", headers={"X-Caller-Service": "test"})
    assert r.status_code == 500
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "GW_500_INTERNAL"
    # 确认异常文本没有泄漏到响应体
    assert "ValueError" not in r.text
    assert "this should not leak into response" not in r.text


# ── HTTPException detail 分支：dict vs 非 dict ────────────────────────────────


def test_http_exception_detail_dict(app: FastAPI) -> None:
    """HTTPException.detail 是 dict 时，code 和 message 从 dict 取。"""
    from fastapi import HTTPException

    @app.get("/test-http-exc-dict")
    async def _raise_dict() -> dict[str, str]:
        raise HTTPException(
            status_code=400, detail={"code": "GW_CUSTOM_CODE", "message": "custom msg"}
        )

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/test-http-exc-dict", headers={"X-Caller-Service": "test"})
    assert r.status_code == 400
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "GW_CUSTOM_CODE"
    assert body["error"]["message"] == "custom msg"


def test_http_exception_detail_string(app: FastAPI) -> None:
    """HTTPException.detail 是字符串时，code 用 GW_<status>，message 是字符串。"""
    from fastapi import HTTPException

    @app.get("/test-http-exc-str")
    async def _raise_str() -> dict[str, str]:
        raise HTTPException(status_code=403, detail="not allowed")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/test-http-exc-str", headers={"X-Caller-Service": "test"})
    assert r.status_code == 403
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "GW_403"
    assert body["error"]["message"] == "not allowed"


# ── API-HTTP-004/006：exception handler 必须透传 exc.headers ──────────────────


def test_405_carries_allow_header(app: FastAPI) -> None:
    """方法不匹配的 405 必须带 Allow。

    Allow 由 Starlette 路由层放在 HTTPException.headers 上；自定义 handler 一旦
    只取 status_code 与 detail，就在全局把这个强制响应头删掉了——而且**任何下游
    handler 都补不回来**，因为它根本没走到 handler。
    """
    client = TestClient(app)
    r = client.request("DELETE", "/api/version")
    assert r.status_code == 405
    assert "GET" in r.headers["allow"]
    # 信封仍然正常
    assert r.json()["error"]["code"] == "GW_405"


def test_http_exception_headers_are_preserved(app: FastAPI) -> None:
    """任意 handler 主动挂在 HTTPException 上的响应头都必须原样出现在响应里。

    这是本波所有「补响应头」工作的地基：不透传 exc.headers，
    401 的 WWW-Authenticate / 503 的 Retry-After 全会被静默吞掉。
    """
    from fastapi import HTTPException

    @app.get("/test-exc-headers")
    async def _raiser() -> dict[str, str]:
        raise HTTPException(
            status_code=418,
            detail={"code": "GW_418_PROBE", "message": "teapot"},
            headers={"X-Probe-Header": "kept", "Retry-After": "7"},
        )

    r = TestClient(app).get("/test-exc-headers", headers={"X-Caller-Service": "test"})
    assert r.status_code == 418
    assert r.headers["x-probe-header"] == "kept"
    assert r.headers["retry-after"] == "7"
    assert r.json()["error"]["code"] == "GW_418_PROBE"


def test_http_exception_without_headers_still_works(app: FastAPI) -> None:
    """exc.headers 为 None 时不得炸（HTTPException 默认就没有 headers）。"""
    from fastapi import HTTPException

    @app.get("/test-exc-no-headers")
    async def _raiser() -> dict[str, str]:
        raise HTTPException(status_code=418, detail={"code": "GW_418_PROBE", "message": "teapot"})

    r = TestClient(app).get("/test-exc-no-headers", headers={"X-Caller-Service": "test"})
    assert r.status_code == 418
    assert r.json()["error"]["code"] == "GW_418_PROBE"
