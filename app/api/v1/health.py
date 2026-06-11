from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.core.context import current_ids
from app.core.envelope import ok

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return ok({"status": "alive"}, trace_id=current_ids().trace_id)


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, Any]:
    checks: dict[str, str] = {}
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        checks["db"] = "not-wired"
    else:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
            checks["db"] = "ok"
    return ok(checks, trace_id=current_ids().trace_id)
