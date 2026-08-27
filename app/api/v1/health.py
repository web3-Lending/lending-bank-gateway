import datetime
import os
from importlib import metadata
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.context import current_ids
from app.core.envelope import err, ok

router = APIRouter(tags=["health"])

# Build-time stamp + git commit SHA baked into the image by deploy/Dockerfile
# (`RUN date -u ... > /srv/build_time.txt && echo "${GIT_SHA}" > /srv/git_sha.txt`).
# Module-level Paths so tests can monkeypatch them to fixture files.
BUILD_TIME_FILE = Path("/srv/build_time.txt")
GIT_SHA_FILE = Path("/srv/git_sha.txt")

# API-HTTP-006 + §7.4「429/503 可计算等待时间时必须返回 Retry-After」。readyz 的两种
# 失败（session_factory 未接线 / DB 探针失败）都由编排层在秒级恢复，给一个保守固定
# 退避值，让调用方按它退避而不是空转打满重试。
READYZ_RETRY_AFTER_SECONDS = "5"
_READYZ_503_HEADERS = {"Retry-After": READYZ_RETRY_AFTER_SECONDS}


def _read_stamp(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _package_app_version() -> str:
    try:
        return metadata.version("lending-bank-gateway")
    except metadata.PackageNotFoundError:
        return "0.1.0"


def _build_time_hkt(build_time: str) -> str | None:
    if not build_time:
        return None
    try:
        dt = datetime.datetime.fromisoformat(build_time.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.UTC)
        hkt = dt.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
        return hkt.strftime("%Y-%m-%d %H:%M:%S HKT")
    except ValueError:
        return build_time


def _runtime_identity() -> dict[str, object]:
    """运行态发布身份（版本号与发布身份规范0707 §/api/version 标准）。

    身份字段全部来自部署链注入的 env（buildops/deploy.sh），镜像 stamp 文件与
    包元数据只作兜底；缺失字段按规范用 version_unknown / not-reported /
    digest_missing / schema_unknown 显式占位，禁止猜测。
    """
    build_time = _read_stamp(BUILD_TIME_FILE)
    git_sha = os.getenv("GIT_SHA") or _read_stamp(GIT_SHA_FILE)
    app_version = os.getenv("APP_VERSION") or _package_app_version()
    return {
        "projectId": os.getenv("PROJECT_ID", "lending-bank-gateway"),
        "serviceName": os.getenv("SERVICE_NAME", "lending-bank-gateway-api"),
        "appVersion": app_version or "version_unknown",
        "releaseId": os.getenv("COLLAB_RELEASE_ID") or "not-reported",
        "releaseRunId": os.getenv("COLLAB_RELEASE_RUN_ID") or "not-reported",
        "releaseEnv": os.getenv("COLLAB_RELEASE_ENV") or os.getenv("GW_ENV") or "local",
        "gitSha": git_sha or "unknown",
        "imageDigest": os.getenv("IMAGE_DIGEST")
        or os.getenv("COLLAB_IMAGE_DIGEST")
        or "digest_missing",
        "sourceConfigDigest": os.getenv("SOURCE_CONFIG_DIGEST") or "digest_missing",
        "schemaRevision": os.getenv("APP_SCHEMA_REVISION")
        or os.getenv("SCHEMA_REVISION")
        or "schema_unknown",
        "buildTimeHkt": os.getenv("BUILD_TIME_HKT") or _build_time_hkt(build_time) or "unknown",
        "dataAction": os.getenv("DATA_ACTION", "none"),
        "dependencies": {
            "mysql": {
                "version": os.getenv("MYSQL_VERSION", "unreported"),
                "status": os.getenv("MYSQL_STATUS", "unreported"),
            }
        },
    }


@router.get("/api/version")
def api_version() -> dict[str, Any]:
    """标准运行态身份端点：data 信封包裹（规范禁止扁平 JSON）。"""
    return ok(_runtime_identity(), trace_id=current_ids().trace_id)


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return ok({"status": "alive"}, trace_id=current_ids().trace_id)


@router.get("/readyz")
async def readyz(request: Request) -> Any:
    checks: dict[str, str] = {}
    trace_id = current_ids().trace_id
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return JSONResponse(
            err("GW_503_READYZ", "session_factory not wired", trace_id=trace_id),
            status_code=503,
            headers=_READYZ_503_HEADERS,
        )
    else:
        try:
            async with factory() as session:
                await session.execute(text("SELECT 1"))
            checks["db"] = "ok"
        except Exception:
            checks["db"] = "error"
            return JSONResponse(
                err("GW_503_READYZ", "db probe failed", trace_id=trace_id),
                status_code=503,
                headers=_READYZ_503_HEADERS,
            )
    return ok(checks, trace_id=trace_id)


@router.get("/build-info")
def build_info() -> dict[str, Any]:
    """Return image build time (UTC ISO-8601) + git commit SHA, envelope-wrapped.

    Both are baked into the image by deploy/Dockerfile. Each returns ``null``
    when its file is absent — e.g. a local dev run outside Docker. ``git_sha``
    anchors the running container to the exact commit it was built from
    (deploy verify step (a)).
    """
    try:
        build_time = BUILD_TIME_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        build_time = ""
    try:
        git_sha = GIT_SHA_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        git_sha = ""
    return ok(
        {"build_time": build_time or None, "git_sha": git_sha or None},
        trace_id=current_ids().trace_id,
    )
