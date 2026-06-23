"""admin 操作端点：outbox 重放等运维接口。"""

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.core.context import current_ids
from app.core.envelope import ok
from app.models.order_alert import OrderStuckAlert
from app.services.outbox import replay_dead

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.post("/outbox/{outbox_id}/replay")
async def replay_outbox(outbox_id: int, request: Request) -> dict[str, object]:
    """将 DEAD 状态的 outbox 行重置为 PENDING 以触发重新投递。

    - 行不存在或非 DEAD 状态 → 404 GW_404_OUTBOX
    - 成功 → ok({"replayed": outbox_id})
    """
    trace_id = current_ids().trace_id
    factory = request.app.state.session_factory
    async with factory() as session:
        async with session.begin():
            replayed = await replay_dead(session, outbox_id=outbox_id)
    if not replayed:
        raise HTTPException(
            404,
            detail={
                "code": "GW_404_OUTBOX",
                "message": f"outbox {outbox_id} not found or not DEAD",
            },
        )
    return ok({"replayed": outbox_id}, trace_id=trace_id)


@router.get("/stuck-orders")
async def list_stuck_orders(request: Request) -> dict[str, object]:
    """G6：列出 order_stuck_alert（超 max_age 未收敛终态的父单告警），供运维处置。

    最多返回最近 200 条（按 first_alerted_at 倒序）。
    """
    trace_id = current_ids().trace_id
    factory = request.app.state.session_factory
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(OrderStuckAlert)
                    .order_by(OrderStuckAlert.first_alerted_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    items = [
        {
            "tenant_id": r.tenant_id,
            "biz_seq_no": r.biz_seq_no,
            "biz_type": r.biz_type,
            "first_alerted_at": r.first_alerted_at.isoformat(),
        }
        for r in rows
    ]
    return ok({"items": items, "count": len(items)}, trace_id=trace_id)
