"""北向 composite-transactions API：composite steps 透传。"""

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import require_headers
from app.core.envelope import ok

router = APIRouter(prefix="/api/v1/composite", tags=["composite"])


@router.get("/{biz_seq_no}/steps")
async def composite_steps(
    request: Request,
    biz_seq_no: str,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    """透传 wedap composite-transactions steps，不做本地 DB 校验。"""
    steps = await request.app.state.wedap.get_composite_steps(
        tenant_id=ids["tenant_id"],
        biz_seq_no=biz_seq_no,
    )
    return ok({"bizSeqNo": biz_seq_no, "steps": steps}, trace_id=ids["trace_id"])
