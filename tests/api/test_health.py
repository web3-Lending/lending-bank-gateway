from fastapi.testclient import TestClient


def test_healthz_ok(app) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["success"] is True and r.json()["trace_id"]


def test_trace_id_echo(app) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    r = client.get("/healthz", headers={"X-Trace-Id": "trc-echo"})
    assert r.json()["trace_id"] == "trc-echo"


def test_trace_id_header_written(app) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    r = client.get("/healthz", headers={"X-Trace-Id": "trc-echo"})
    assert r.headers["x-trace-id"] == "trc-echo"


def test_context_propagated_to_handler(app) -> None:  # type: ignore[no-untyped-def]
    from app.core.context import current_ids

    received: list[str] = []

    @app.get("/test-ctx")
    async def ctx_probe() -> dict:  # type: ignore[misc]
        received.append(current_ids().trace_id)
        return {}

    client = TestClient(app)
    # /test-ctx 不在豁免列表，需带 X-Caller-Service
    client.get("/test-ctx", headers={"X-Trace-Id": "trc-probe", "X-Caller-Service": "test"})
    assert received == ["trc-probe"]


def test_readyz_db_ok(app) -> None:  # type: ignore[no-untyped-def]
    """create_app 已接 sqlite 内存引擎，/readyz 应返回 db=ok。"""
    client = TestClient(app)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["data"]["db"] == "ok"


async def test_readyz_db_ok_direct(app) -> None:  # type: ignore[no-untyped-def]
    """直接调用 handler 确保 async with session 内部代码被 coverage 追踪到。"""
    from app.api.v1.health import readyz

    class _State:
        session_factory = app.state.session_factory

    class _FakeApp:
        state = _State()

    class _FakeRequest:
        app = _FakeApp()

    result = await readyz(_FakeRequest())  # type: ignore[arg-type]
    assert result["data"]["db"] == "ok"


def test_readyz_db_not_wired() -> None:
    """无 session_factory 时返回 not-wired。"""
    from fastapi import FastAPI

    from app.core.context import IdentifierMiddleware

    bare = FastAPI()
    bare.add_middleware(IdentifierMiddleware)

    from app.api.v1.health import router as health_router

    bare.include_router(health_router)
    client = TestClient(bare)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["data"]["db"] == "not-wired"
