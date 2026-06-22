"""统一终态收口（V2 同步优先）：同步 / 回调 / 兜底三路径共用。

订单 status 被推进到终态后，本 helper 在**调用方事务内**幂等地：
  1. set finalized_at + finalized_via（来源留痕 SYNC/CALLBACK/RECONCILE）
  2. write_audit(ORDER_<终态>)
  3. enqueue_forward → lifecycle，dedup_key 用**业务稳定键** fwd-{tenant}-{biz}-{status}

幂等关键（codex HIGH-2 / MED-3）：
  - order.finalized_at 已非空 → 直接 return，**防同步终态 + 回调双收口重复转发**。
  - 稳定 dedup_key 跨路径一致 → outbox 去重，lifecycle 只收一次终态事件；
    且该 key 作下游 X-Request-Id 保证下游幂等（替代易分叉的 fwd-{request_id}）。
  - 仅**终态**（SUCCEEDED/FAILED）转发；非终态（SUBMITTED/PROCESSING）不转发。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.domain.states import OrderStatus
from app.models.txn import BankTxnOrder
from app.services.audit import write_audit
from app.services.outbox import enqueue_forward

# 触发收口（finalized_at/via + 转发）的终态集合：wedap 同步/回调的两类业务终态。
TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset({OrderStatus.SUCCEEDED, OrderStatus.FAILED})


def is_terminal(status: str) -> bool:
    """status 是否属于触发终态收口的集合（SUCCEEDED/FAILED）。"""
    return OrderStatus(status) in TERMINAL_STATUSES


def terminal_event(biz_seq_no: str, status: str, source: str) -> dict[str, Any]:
    """gateway 派生终态事件（统一转发体，三路径一致；替代「转发原始回调 body」的语义分叉）。"""
    return {"bizSeqNo": biz_seq_no, "txnStatus": str(status), "finalizedVia": source}


async def finalize_terminal_in_session(
    session: Any,
    *,
    order: BankTxnOrder,
    source: str,
    trace_id: str,
    caller_service: str = "gateway",
) -> None:
    """order.status 已被推进到终态后调用：幂等收口（finalized_at/via + audit + 转发）。

    前置：调用方已把 order.status 设为终态并持有该 order 行（同事务）。
    幂等：order.finalized_at 已非空 → 直接 return（同步与回调任一先收口，后到者跳过）。
    """
    if order.finalized_at is not None:
        return
    order.finalized_at = dt.datetime.now(dt.UTC)
    order.finalized_via = source
    await write_audit(
        session,
        tenant_id=order.tenant_id,
        actor=f"svc:{caller_service}",
        action=f"ORDER_{order.status}",
        entity=f"bank_txn_order:{order.biz_seq_no}",
        payload={"finalized_via": source},
    )
    await enqueue_forward(
        session,
        tenant_id=order.tenant_id,
        target="lifecycle",
        payload=terminal_event(order.biz_seq_no, order.status, source),
        dedup_key=f"fwd-{order.tenant_id}-{order.biz_seq_no}-{order.status}",
        trace_id=trace_id,
    )
