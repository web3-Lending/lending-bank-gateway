"""南向银行回调接收 API：wedap 交易通知入站幂等收录。"""

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy.exc import IntegrityError

from app.api.deps import require_headers
from app.core.envelope import ok
from app.models.callback import CallbackInbox

router = APIRouter(prefix="/api/v1/callbacks/wedap", tags=["callbacks"])


async def _noop_after_ingest(request: Request, *, tenant_id: str, body: dict[str, Any]) -> None:
    """T16/T17 接线点：leg 同步 + outbox 转发。本任务为空实现。"""


@router.post("/transactions")
async def wedap_transaction_callback(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    hdr = require_headers(request)
    factory = request.app.state.session_factory
    dedup = False
    try:
        async with factory() as session:
            async with session.begin():
                session.add(
                    CallbackInbox(
                        tenant_id=hdr["tenant_id"],
                        source="WEDAP_TXN",
                        request_id=hdr["request_id"],
                        payload=body,
                    )
                )
    except IntegrityError:
        dedup = True
    if not dedup:
        after_ingest = getattr(request.app.state, "callback_after_ingest", _noop_after_ingest)
        await after_ingest(request, tenant_id=hdr["tenant_id"], body=body)
    return ok({"received": True, "deduplicated": dedup}, trace_id=hdr["trace_id"])
