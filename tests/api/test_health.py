from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.context import IdentifierMiddleware


def test_healthz_ok(app: FastAPI) -> None:
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["success"] is True and r.json()["trace_id"]


def test_trace_id_echo(app: FastAPI) -> None:
    client = TestClient(app)
    r = client.get("/healthz", headers={"X-Trace-Id": "trc-echo"})
    assert r.json()["trace_id"] == "trc-echo"


def test_trace_id_header_written(app: FastAPI) -> None:
    client = TestClient(app)
    r = client.get("/healthz", headers={"X-Trace-Id": "trc-echo"})
    assert r.headers["x-trace-id"] == "trc-echo"


def test_context_propagated_to_handler(app: FastAPI) -> None:
    from app.core.context import current_ids

    received: list[str] = []

    @app.get("/test-ctx")
    async def ctx_probe() -> dict[str, Any]:
        received.append(current_ids().trace_id)
        return {}

    client = TestClient(app)
    # /test-ctx 不在豁免列表，需带 X-Caller-Service
    client.get("/test-ctx", headers={"X-Trace-Id": "trc-probe", "X-Caller-Service": "test"})
    assert received == ["trc-probe"]


def test_readyz_db_ok(app: FastAPI) -> None:
    """create_app 已接 sqlite 内存引擎，/readyz 应返回 db=ok。"""
    client = TestClient(app)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["data"]["db"] == "ok"


# MIN-1：test_readyz_db_ok（TestClient 路径）已覆盖 async with session 内部代码，
# test_readyz_db_ok_direct（直调 handler）不再需要，已删除。


def test_readyz_db_not_wired() -> None:
    """无 session_factory 时返回 503 + err envelope（fail-closed）。"""
    bare = FastAPI()
    bare.add_middleware(IdentifierMiddleware)

    from app.api.v1.health import router as health_router

    bare.include_router(health_router)
    client = TestClient(bare)
    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "GW_503_READYZ"


def test_readyz_db_error_returns_503() -> None:
    """DB 探测抛异常时 /readyz 应返回 503，走 err envelope。"""
    bare = FastAPI()
    bare.add_middleware(IdentifierMiddleware)

    from app.api.v1.health import router as health_router

    bare.include_router(health_router)

    # 注入会抛异常的 fake session factory（asynccontextmanager 在 __aenter__ 前抛）
    @asynccontextmanager  # type: ignore[arg-type]
    async def _bad_factory() -> Any:
        raise RuntimeError("db connection failed")
        yield  # type: ignore[misc]  # noqa: B901

    bare.state.session_factory = _bad_factory

    client = TestClient(bare, raise_server_exceptions=False)
    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "GW_503_READYZ"
