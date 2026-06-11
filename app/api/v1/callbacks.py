"""南向银行回调接收 API：wedap 交易通知入站幂等收录。

幂等设计：
- 三元组 (tenant_id, source, request_id) 唯一约束（uq_inbox_tenant_src_req）防重落库
- IntegrityError 收窄：仅拦截唯一约束冲突；其它约束违反（FK/CHECK/非空等）re-raise → 500
- after_ingest 失败补偿：落库后 try/except 隔离，失败留 status=RECEIVED + error 分类，响应仍 200
- 重放再驱动：dedup 路径查既有行 status：
    - RECEIVED（上次未完成）→ 再次执行 after_ingest；成功推进 PROCESSED
    - PROCESSED（已完成）→ 纯去重返回，不重复驱动
  这样 wedap 的自动重试天然成为补偿驱动；超出 wedap 重试次数的滞留 RECEIVED 行
  由 T17 worker/admin 重放收敛。
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import require_headers
from app.core.envelope import ok
from app.models.callback import CallbackInbox

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/callbacks/wedap", tags=["callbacks"])

# 唯一约束名（MySQL）与 SQLite UNIQUE constraint failed 字段路径
_DEDUP_CONSTRAINT = "uq_inbox_tenant_src_req"
_DEDUP_SQLITE_FRAGMENT = "callback_inbox.tenant_id"


def _is_dedup_error(exc: IntegrityError) -> bool:
    """判别 IntegrityError 是否来自 inbox 三元组唯一约束。

    MySQL：orig 消息含约束名 uq_inbox_tenant_src_req
    SQLite：orig 消息含 UNIQUE constraint failed: callback_inbox.tenant_id, ...
    """
    msg = str(exc.orig)
    return _DEDUP_CONSTRAINT in msg or _DEDUP_SQLITE_FRAGMENT in msg


async def _get_inbox_row(factory: Any, tenant_id: str, request_id: str) -> CallbackInbox | None:
    """查询 inbox 既有行（不开事务，只读）。"""
    async with factory() as session:
        result = await session.execute(
            select(CallbackInbox).where(
                CallbackInbox.tenant_id == tenant_id,
                CallbackInbox.source == "WEDAP_TXN",
                CallbackInbox.request_id == request_id,
            )
        )
        return result.scalars().first()  # type: ignore[no-any-return]


async def _set_inbox_status(
    factory: Any,
    tenant_id: str,
    request_id: str,
    *,
    status: str,
    error: str | None,
) -> None:
    """在独立事务内更新 inbox 行的 status 与 error 字段。

    行刚插入后立即调用，必然存在；用 scalars().one() 精确语义。
    """
    async with factory() as session:
        async with session.begin():
            result = await session.execute(
                select(CallbackInbox).where(
                    CallbackInbox.tenant_id == tenant_id,
                    CallbackInbox.source == "WEDAP_TXN",
                    CallbackInbox.request_id == request_id,
                )
            )
            row = result.scalars().one()
            row.status = status
            row.error = error


async def _noop_after_ingest(request: Request, *, tenant_id: str, body: dict[str, Any]) -> None:
    """T16/T17 接线点：leg 同步 + outbox 转发。本任务为空实现。"""


@router.post("/transactions")
async def wedap_transaction_callback(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    hdr = require_headers(request)

    # --- 最小 body 校验（插入前，不落库）---
    if not body.get("bizSeqNo"):
        raise HTTPException(
            400,
            detail={"code": "GW_400_VALIDATION", "message": "missing bizSeqNo"},
        )
    if not body.get("type"):
        raise HTTPException(
            400,
            detail={"code": "GW_400_VALIDATION", "message": "missing type"},
        )

    factory = request.app.state.session_factory
    after_ingest = getattr(request.app.state, "callback_after_ingest", _noop_after_ingest)
    dedup = False

    # --- 首次入库 ---
    try:
        async with factory() as session:
            async with session.begin():
                session.add(
                    CallbackInbox(
                        tenant_id=hdr["tenant_id"],
                        source="WEDAP_TXN",
                        request_id=hdr["request_id"],
                        payload=body,
                        status="RECEIVED",
                    )
                )
    except IntegrityError as exc:
        if not _is_dedup_error(exc):
            # 非唯一约束违反（FK/CHECK/NOT NULL 等）→ 透传 500
            raise
        dedup = True

    if not dedup:
        # --- 正常路径：after_ingest 失败补偿 ---
        try:
            await after_ingest(request, tenant_id=hdr["tenant_id"], body=body)
            # after_ingest 成功 → 推进 status=PROCESSED
            await _set_inbox_status(
                factory, hdr["tenant_id"], hdr["request_id"], status="PROCESSED", error=None
            )
        except Exception:
            # after_ingest 失败：记录 error 分类，status 留 RECEIVED，响应仍 200
            logger.exception(
                "after_ingest failed tenant=%s request_id=%s",
                hdr["tenant_id"],
                hdr["request_id"],
            )
            await _set_inbox_status(
                factory,
                hdr["tenant_id"],
                hdr["request_id"],
                status="RECEIVED",
                error="after_ingest_failed",
            )
    else:
        # --- dedup 路径：重放再驱动 ---
        # 查既有行 status：RECEIVED → 再驱动；PROCESSED → 纯去重
        existing = await _get_inbox_row(factory, hdr["tenant_id"], hdr["request_id"])

        if existing is not None and existing.status == "RECEIVED":
            # 上次 after_ingest 未完成 → 重放再驱动
            try:
                await after_ingest(request, tenant_id=hdr["tenant_id"], body=body)
                await _set_inbox_status(
                    factory, hdr["tenant_id"], hdr["request_id"], status="PROCESSED", error=None
                )
            except Exception:
                logger.exception(
                    "after_ingest replay failed tenant=%s request_id=%s",
                    hdr["tenant_id"],
                    hdr["request_id"],
                )

    return ok({"received": True, "deduplicated": dedup}, trace_id=hdr["trace_id"])
