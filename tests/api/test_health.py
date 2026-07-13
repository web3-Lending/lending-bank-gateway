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


def test_api_version_env_driven_identity(app: FastAPI, tmp_path: Any, monkeypatch: Any) -> None:
    """部署链 env 全注入 → data 信封逐字段回显（规范 §/api/version 标准）。"""
    from app.api.v1 import health as health_mod

    for key, value in {
        "APP_VERSION": "0.1.0",
        "GIT_SHA": "a" * 40,
        "PROJECT_ID": "lending-bank-gateway",
        "SERVICE_NAME": "lending-bank-gateway-api",
        "COLLAB_RELEASE_ID": "lending-bank-gateway-20260713-001",
        "COLLAB_RELEASE_RUN_ID": "dev-promote-20260713-001",
        "COLLAB_RELEASE_ENV": "dev",
        "IMAGE_DIGEST": "sha256:" + "b" * 64,
        "SOURCE_CONFIG_DIGEST": "sha256:" + "c" * 64,
        "APP_SCHEMA_REVISION": "0007_leg_unique",
        "BUILD_TIME_HKT": "2026-07-13 18:00:00 HKT",
        "DATA_ACTION": "none",
    }.items():
        monkeypatch.setenv(key, value)
    bt = tmp_path / "build_time.txt"
    bt.write_text("2026-07-13T10:00:00Z\n", encoding="utf-8")
    with patch.object(health_mod, "BUILD_TIME_FILE", bt):
        client = TestClient(app)
        r = client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["trace_id"]
    data = body["data"]
    assert data["appVersion"] == "0.1.0"
    assert data["gitSha"] == "a" * 40
    assert data["releaseId"] == "lending-bank-gateway-20260713-001"
    assert data["releaseRunId"] == "dev-promote-20260713-001"
    assert data["releaseEnv"] == "dev"
    assert data["imageDigest"] == "sha256:" + "b" * 64
    assert data["sourceConfigDigest"] == "sha256:" + "c" * 64
    assert data["schemaRevision"] == "0007_leg_unique"
    assert data["buildTimeHkt"] == "2026-07-13 18:00:00 HKT"
    assert data["dataAction"] == "none"
    assert data["dependencies"]["mysql"] == {
        "version": "unreported",
        "status": "unreported",
    }


def test_api_version_fallbacks_without_env(app: FastAPI, tmp_path: Any, monkeypatch: Any) -> None:
    """无部署 env → 包元数据版本 + 显式占位（不猜测、不假绿）。"""
    from app.api.v1 import health as health_mod

    for key in (
        "APP_VERSION",
        "GIT_SHA",
        "PROJECT_ID",
        "SERVICE_NAME",
        "COLLAB_RELEASE_ID",
        "COLLAB_RELEASE_RUN_ID",
        "COLLAB_RELEASE_ENV",
        "GW_ENV",
        "IMAGE_DIGEST",
        "COLLAB_IMAGE_DIGEST",
        "SOURCE_CONFIG_DIGEST",
        "APP_SCHEMA_REVISION",
        "SCHEMA_REVISION",
        "BUILD_TIME_HKT",
        "DATA_ACTION",
    ):
        monkeypatch.delenv(key, raising=False)
    missing = tmp_path / "absent.txt"
    with (
        patch.object(health_mod, "BUILD_TIME_FILE", missing),
        patch.object(health_mod, "GIT_SHA_FILE", missing),
    ):
        client = TestClient(app)
        r = client.get("/api/version")
    data = r.json()["data"]
    assert data["projectId"] == "lending-bank-gateway"
    assert data["serviceName"] == "lending-bank-gateway-api"
    assert data["appVersion"] == "0.1.0"
    assert data["releaseId"] == "not-reported"
    assert data["releaseRunId"] == "not-reported"
    assert data["releaseEnv"] == "local"
    assert data["gitSha"] == "unknown"
    assert data["imageDigest"] == "digest_missing"
    assert data["sourceConfigDigest"] == "digest_missing"
    assert data["schemaRevision"] == "schema_unknown"
    assert data["buildTimeHkt"] == "unknown"


def test_api_version_package_metadata_fallback(monkeypatch: Any) -> None:
    """importlib.metadata 未安装包 → 常量兜底 0.1.0。"""
    from importlib import metadata as importlib_metadata

    from app.api.v1 import health as health_mod

    def _raise(_: str) -> str:
        raise importlib_metadata.PackageNotFoundError

    monkeypatch.setattr(health_mod.metadata, "version", _raise)
    assert health_mod._package_app_version() == "0.1.0"


def test_api_version_build_time_hkt_conversion() -> None:
    """UTC stamp → HKT 展示；空值 → None；非法值原样透传（规范：只展示不排序）。"""
    from app.api.v1 import health as health_mod

    assert health_mod._build_time_hkt("2026-07-13T10:00:00Z") == "2026-07-13 18:00:00 HKT"
    assert health_mod._build_time_hkt("2026-07-13T10:00:00") == "2026-07-13 18:00:00 HKT"
    assert health_mod._build_time_hkt("") is None
    assert health_mod._build_time_hkt("not-a-timestamp") == "not-a-timestamp"


def test_api_version_exempt_from_s2s(monkeypatch: Any) -> None:
    """/api/version 在 S2S 豁免清单——无 token 也可读版本身份（与 /build-info 同权）。"""
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("GW_ENV", "test")
    monkeypatch.delenv("COLLAB_RELEASE_ENV", raising=False)
    get_settings.cache_clear()
    real_app = create_app()
    client = TestClient(real_app)
    r = client.get("/api/version")
    assert r.status_code == 200
    assert r.json()["data"]["releaseEnv"] == "test"
