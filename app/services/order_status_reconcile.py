"""G2：RESULT_UNKNOWN / 非终态父单经 wedap status-query 主动复查收敛（order 级，C5）。

本服务调 wedap.query_transaction_status（通用 `GET /api/v1/transactions/status`，
对接文档 v0.3.0 §5.5，真实现）直接查父单终态：

- 旧 per-type 状态端点是桩（假单也回 SUCCESS，W7）——曾把资金半程滞留单假收敛成
  SUCCEEDED（2026-07-14 Round 1 双样本实锤，FU-GW-RECONCILE-FALSE-FINALIZE），已弃用。
- 通用接口四必填供参存于 order（0020）：trans_type（提交原值）+ ori_req_date（提交日）。
  存量行两列 NULL → 返 False 不回查（交 G6 stuck alert 人工处置），不做 biz_type 反推
  兜底——反推错了就是又一轮假终态。
- 事务外查 wedap；事务内 FOR UPDATE 重读父单做 CAS：已终态 / finalized_at 非空 → 跳过
  （防非法迁移与双转发）
- 仅当前态非终态且映射为终态（含 REVERSED，counter 人工冲正回传）时
  assert_transition + finalize（复用同步/回调同一收口）
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.wedap import WedapError
from app.domain.states import (
    ABSORBING_STATUSES,
    IllegalTransition,
    OrderStatus,
    assert_transition,
    map_wedap_txn_status,
)
from app.models.txn import BankTxnOrder
from app.services.order_finalize import finalize_terminal_in_session

logger = logging.getLogger(__name__)


async def resolve_terminal_via_status_query(
    factory: async_sessionmaker[Any],
    *,
    wedap: Any,
    tenant_id: str,
    biz_seq_no: str,
    source: str = "RECONCILE",
) -> bool:
    """对非终态父单经 wedap status-query 收敛终态。

    返回 True=本次收敛到终态；False=未收敛（不支持 / 暂态 / 已终态 / 非终态结果）。
    """
    # 1. 事务外轻量读通用回查供参（trans_type/ori_req_date，0020）
    async with factory() as session:
        order = (
            await session.execute(
                select(BankTxnOrder).where(
                    BankTxnOrder.tenant_id == tenant_id,
                    BankTxnOrder.biz_seq_no == biz_seq_no,
                )
            )
        ).scalar_one_or_none()
        if order is None:
            return False
        trans_type = order.trans_type
        ori_req_date = order.ori_req_date

    # 供参缺失（0020 前存量单）→ 不回查（交 G6 升级），禁 biz_type 反推防假终态
    if not trans_type or not ori_req_date:
        logger.warning(
            "status-reconcile skip %s/%s: missing trans_type/ori_req_date (pre-0020 order)",
            tenant_id,
            biz_seq_no,
        )
        return False

    # 2. 事务外查 wedap；wedap 业务错误 / 暂态 HTTP 错误 → 不收敛，交下轮 / G6
    try:
        data = await wedap.query_transaction_status(
            tenant_id=tenant_id,
            request_id=f"status-reconcile-{biz_seq_no}",
            ori_biz_seq_no=biz_seq_no,
            trans_type=trans_type,
            ori_req_date=ori_req_date,
        )
    except (WedapError, httpx.HTTPError):
        return False

    # 响应身份核对（codex P1）：oriBizSeqNo/transType 必须与请求一致才允许推进——
    # 防 wedap 侧任何形式的串单/错路由把别人的终态收到本单头上。缺失视同不一致（保守，
    # 不收敛交下轮/G6，方向安全）。
    resp_ori = str(data.get("oriBizSeqNo") or "")
    resp_type = str(data.get("transType") or "")
    if resp_ori != biz_seq_no or resp_type != trans_type:
        logger.error(
            "status-reconcile identity mismatch %s/%s: resp ori=%s type=%s (expect type=%s)",
            tenant_id,
            biz_seq_no,
            resp_ori,
            resp_type,
            trans_type,
        )
        return False

    terminal = map_wedap_txn_status(str(data.get("txnStatus", "")))
    if terminal is None:
        return False  # 非终态结果 → no-op，等下轮

    # 3. 事务内 FOR UPDATE 重读 + CAS：已终态 / 已收口 → 跳过，防非法迁移与双转发
    async with factory() as session:
        async with session.begin():
            locked = (
                await session.execute(
                    select(BankTxnOrder)
                    .where(
                        BankTxnOrder.tenant_id == tenant_id,
                        BankTxnOrder.biz_seq_no == biz_seq_no,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None:  # pragma: no cover - 两读之间被删，竞态防御
                return False
            if locked.finalized_at is not None or OrderStatus(locked.status) in ABSORBING_STATUSES:
                # 终态升级（reversal ingestion）：SUCCEEDED 已收口单查询到 REVERSED =
                # counter 冲正（§3.6）——推进 + 状态级二次收口；重复事件由「status 已是
                # REVERSED」幂等挡住。其余已收口场景照旧跳过（防倒退/双转发）。
                if (
                    OrderStatus(locked.status) == OrderStatus.SUCCEEDED
                    and terminal == OrderStatus.REVERSED
                ):
                    assert_transition(OrderStatus(locked.status), terminal)
                    locked.status = terminal
                    await finalize_terminal_in_session(
                        session,
                        order=locked,
                        source=source,
                        trace_id=f"reconcile-{biz_seq_no}",
                        upgrade_from=str(OrderStatus.SUCCEEDED),
                    )
                    return True
                return False
            try:
                assert_transition(OrderStatus(locked.status), terminal)
            except IllegalTransition:  # pragma: no cover - 非终态候选→终态均合法，防御分支
                logger.warning(
                    "status-reconcile illegal transition %s/%s %s->%s",
                    tenant_id,
                    biz_seq_no,
                    locked.status,
                    terminal,
                )
                return False
            locked.status = terminal
            await finalize_terminal_in_session(
                session,
                order=locked,
                source=source,
                trace_id=f"reconcile-{biz_seq_no}",
            )
    return True
