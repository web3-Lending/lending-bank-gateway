"""admin 操作端点：outbox 重放等运维接口。"""

import datetime as dt

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select

from app.core.context import current_ids
from app.core.envelope import ok
from app.models.order_alert import OrderStuckAlert
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.models.wedap_delivery_alert import WedapDeliveryAlert
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


def _alert_counts_query(import_date: str | None):  # type: ignore[no-untyped-def]
    """alerts 按 kind 计数；import_date 过滤时与投递统计同口径（codex MEDIUM）——
    join 回投递表按 (tenant_id, import_batch_no) 关联，避免切流日报混入其它日期的告警。"""
    q = select(WedapDeliveryAlert.kind, func.count()).group_by(WedapDeliveryAlert.kind)
    if import_date is None:
        return q
    return q.join(
        WedapImportDeliveryTask,
        (WedapImportDeliveryTask.tenant_id == WedapDeliveryAlert.tenant_id)
        & (WedapImportDeliveryTask.import_batch_no == WedapDeliveryAlert.import_batch_no),
    ).where(WedapImportDeliveryTask.import_date == import_date)


@router.get("/wedap-import/delivery-report")
async def wedap_delivery_report(
    request: Request, import_date: str | None = None
) -> dict[str, object]:
    """§6.1 护栏⑤：wedap 投递报告——gateway acceptance 与 result closure 分开统计。

    acceptance（受理面）：按 status 计数 + 已受理数（accepted_at 非空）。
    result_closure（结果闭环面）：DELIVERED 中已回收 / 未回收 / 超截止未回收（overdue）。
    alerts：wedap_delivery_alert 按 kind 计数（PENDING_STUCK / RESULT_OVERDUE）。
    两面分开呈现，"notify 受理成功"不再被误读为"批次已闭环"。可选 import_date（YYYYMMDD）过滤。
    """
    trace_id = current_ids().trace_id
    factory = request.app.state.session_factory
    now = dt.datetime.now(dt.UTC)
    conds = []
    if import_date is not None:
        conds.append(WedapImportDeliveryTask.import_date == import_date)
    async with factory() as session:
        status_rows = (
            await session.execute(
                select(WedapImportDeliveryTask.status, func.count())
                .where(*conds)
                .group_by(WedapImportDeliveryTask.status)
            )
        ).all()
        accepted = (
            await session.execute(
                select(func.count())
                .select_from(WedapImportDeliveryTask)
                .where(WedapImportDeliveryTask.accepted_at.is_not(None), *conds)
            )
        ).scalar_one()
        collected = (
            await session.execute(
                select(func.count())
                .select_from(WedapImportDeliveryTask)
                .where(
                    WedapImportDeliveryTask.status == "DELIVERED",
                    WedapImportDeliveryTask.result_collected_at.is_not(None),
                    *conds,
                )
            )
        ).scalar_one()
        outstanding = (
            await session.execute(
                select(func.count())
                .select_from(WedapImportDeliveryTask)
                .where(
                    WedapImportDeliveryTask.status == "DELIVERED",
                    WedapImportDeliveryTask.result_collected_at.is_(None),
                    *conds,
                )
            )
        ).scalar_one()
        overdue = (
            await session.execute(
                select(func.count())
                .select_from(WedapImportDeliveryTask)
                .where(
                    WedapImportDeliveryTask.status == "DELIVERED",
                    WedapImportDeliveryTask.result_collected_at.is_(None),
                    WedapImportDeliveryTask.result_deadline_at.is_not(None),
                    WedapImportDeliveryTask.result_deadline_at <= now,
                    *conds,
                )
            )
        ).scalar_one()
        alert_rows = (await session.execute(_alert_counts_query(import_date))).all()
    status_counts = {status: count for status, count in status_rows}
    return ok(
        {
            "acceptance": {
                "by_status": status_counts,
                "total": sum(status_counts.values()),
                "accepted": accepted,
            },
            "result_closure": {
                "delivered": status_counts.get("DELIVERED", 0),
                "result_collected": collected,
                "outstanding": outstanding,
                "overdue": overdue,
            },
            "alerts": {kind: count for kind, count in alert_rows},
            "import_date": import_date,
        },
        trace_id=trace_id,
    )
