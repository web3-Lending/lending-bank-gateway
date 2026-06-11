from typing import Any

from fastapi import APIRouter

from app.core.context import current_ids
from app.core.envelope import ok

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return ok({"status": "alive"}, trace_id=current_ids().trace_id)
