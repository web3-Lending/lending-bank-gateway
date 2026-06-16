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
