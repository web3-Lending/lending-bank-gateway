from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

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


def test_build_info_returns_build_time_and_git_sha(app: FastAPI, tmp_path: Any) -> None:
    """Both stamp files present → /build-info echoes them in the success envelope."""
    from app.api.v1 import health as health_mod

    bt = tmp_path / "build_time.txt"
    bt.write_text("2026-05-20T06:14:08Z\n", encoding="utf-8")
    sha = tmp_path / "git_sha.txt"
    sha.write_text("d084d91\n", encoding="utf-8")
    with (
        patch.object(health_mod, "BUILD_TIME_FILE", bt),
        patch.object(health_mod, "GIT_SHA_FILE", sha),
    ):
        client = TestClient(app)
        r = client.get("/build-info")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"] == {"build_time": "2026-05-20T06:14:08Z", "git_sha": "d084d91"}
    assert body["trace_id"]


def test_build_info_git_sha_dirty_marker(app: FastAPI, tmp_path: Any) -> None:
    """A dirty working-tree build stamps ``<sha>-dirty``; echoed verbatim so
    deploy verify can tell an uncommitted build from a clean one."""
    from app.api.v1 import health as health_mod

    sha = tmp_path / "git_sha.txt"
    sha.write_text("d084d91-dirty\n", encoding="utf-8")
    with patch.object(health_mod, "GIT_SHA_FILE", sha):
        client = TestClient(app)
        r = client.get("/build-info")
    assert r.json()["data"]["git_sha"] == "d084d91-dirty"


def test_build_info_null_when_files_absent(app: FastAPI, tmp_path: Any) -> None:
    """Missing stamp files (local dev outside Docker) → both null."""
    from app.api.v1 import health as health_mod

    missing_bt = tmp_path / "no_build_time.txt"
    missing_sha = tmp_path / "no_git_sha.txt"
    with (
        patch.object(health_mod, "BUILD_TIME_FILE", missing_bt),
        patch.object(health_mod, "GIT_SHA_FILE", missing_sha),
    ):
        client = TestClient(app)
        r = client.get("/build-info")
    assert r.status_code == 200
    assert r.json()["data"] == {"build_time": None, "git_sha": None}


def test_build_info_null_when_files_empty(app: FastAPI, tmp_path: Any) -> None:
    """Empty/whitespace stamp files → null, not empty string."""
    from app.api.v1 import health as health_mod

    bt = tmp_path / "build_time.txt"
    bt.write_text("  \n", encoding="utf-8")
    sha = tmp_path / "git_sha.txt"
    sha.write_text("   \n", encoding="utf-8")
    with (
        patch.object(health_mod, "BUILD_TIME_FILE", bt),
        patch.object(health_mod, "GIT_SHA_FILE", sha),
    ):
        client = TestClient(app)
        r = client.get("/build-info")
    assert r.json()["data"] == {"build_time": None, "git_sha": None}
