"""北向银行资金 API：collect-from-users + distribute-to-users + status。"""

from decimal import Decimal
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import (
    assert_idempotency_key_matches,
    parse_amount,
    require_headers,
    validate_detail_consistency,
)
from app.clients.wedap import WedapError
from app.core.envelope import ok
from app.models.txn import BankTxnOrder
from app.services.idempotency import IdempotencyConflict
from app.services.submit import SubmitRequest, submit_order

router = APIRouter(prefix="/api/v1/bank-funds", tags=["bank-funds"])


# ── Pydantic request schemas ───────────────────────────────────────────────────


class CollectRequest(BaseModel):
    # 归集对齐 wedap 真契约：单用户扁平，顶层 txnAmount + bankAccountName 必填。
    # extra=allow 薄透传：lending 补 bankAccountName/userId 等原样透传 wedap，gateway 不剪裁。
    # 金额优先扁平 txnAmount（wedap 形态）；过渡期回退 totalAmount，lending 改扁平后只用 txnAmount。
    model_config = ConfigDict(extra="allow")
    bizSeqNo: str
    currencyCode: str
    txnAmount: str | None = None
    totalAmount: str | None = None


class DistributeRequest(BaseModel):
    # 分发薄透传：wedap body = 顶层 currencyCode + recipients[].distributeAmount（无 totalAmount）。
    # recipients 各项的 userId/custAccountNo/bankAccountNo/vaultId 等经 extra=allow 原样透传。
    model_config = ConfigDict(extra="allow")
    bizSeqNo: str
    currencyCode: str
    recipients: list[dict[str, Any]] | None = None
    """显式 null 视同缺省，契约 C 下 wedap 可选字段缺省=null 语义等价。"""


# ── 内部提交 helper ────────────────────────────────────────────────────────────


async def _submit(
    request: Request,
    *,
    ids: dict[str, str],
    biz_seq_no: str,
    business_action: str,
    biz_type: str,
    business_scope: str,
    wedap_method: str,
    amount: Decimal,
    currency: str,
    wedap_payload: dict[str, Any],
) -> dict[str, Any]:
    """validate biz_seq_no → submit_order → catch IdempotencyConflict → ok envelope。"""
    try:
        result = await submit_order(
            request.app.state.session_factory,
            wedap_call=getattr(request.app.state.wedap, wedap_method),
            req=SubmitRequest(
                tenant_id=ids["tenant_id"],
                biz_seq_no=biz_seq_no,
                business_action=business_action,
                biz_type=biz_type,
                amount=amount,
                currency=currency,
                caller_service=ids["caller_service"],
                request_id=ids["request_id"],
                business_scope=business_scope,
                wedap_payload=wedap_payload,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            400,
            detail={"code": "GW_400_VALIDATION", "message": str(exc)},
        ) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(
            409,
            detail={"code": "GW_409_IDEMPOTENCY", "message": f"idempotency conflict: {exc}"},
        ) from exc
    return ok(result, trace_id=ids["trace_id"])


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/collect-from-users")
async def collect_from_users(
    body: CollectRequest,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    assert_idempotency_key_matches(request, body.bizSeqNo)
    payload = body.model_dump(mode="json", exclude_none=True)
    # 金额优先 wedap 扁平 txnAmount，过渡回退 totalAmount；空串/缺失视为缺。归集单用户，不 sum 校验。
    raw_amount = body.txnAmount or body.totalAmount
    if not raw_amount:
        raise HTTPException(
            400,
            detail={"code": "GW_400_VALIDATION", "message": "missing txnAmount"},
        )
    amount = parse_amount(raw_amount)
    # txnAmount 在场时 totalAmount 属旧形态噪声字段，不透传 wedap（避免注入伪字段 + 幂等漂移）
    if body.txnAmount and "totalAmount" in payload:
        payload.pop("totalAmount")
    return await _submit(
        request,
        ids=ids,
        biz_seq_no=body.bizSeqNo,
        business_action="COLLECT",
        biz_type="CLT",
        business_scope="bank_collect",
        wedap_method="collect_from_users",
        amount=amount,
        currency=body.currencyCode,
        wedap_payload=payload,
    )


@router.post("/distribute-to-users")
async def distribute_to_users(
    body: DistributeRequest,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    assert_idempotency_key_matches(request, body.bizSeqNo)
    payload = body.model_dump(mode="json", exclude_none=True)
    # 分发 wedap 契约无顶层总额：本地账本/幂等金额 = Σ recipients[].distributeAmount。
    # distributeAmount 缺省/null 一致视为「wedap 自动分配」跳过求和（避免 str(None) 误炸 400）。
    recipients = body.recipients or []
    amount = sum(
        (
            parse_amount(str(r["distributeAmount"]))
            for r in recipients
            if r.get("distributeAmount") is not None
        ),
        Decimal("0"),
    )
    # total=None：分发无独立顶层总额，validate 仅做「非空 + 币种一致」，不做同义重复的 sum 校验
    validate_detail_consistency(
        payload,
        total=None,
        currency=body.currencyCode,
        detail_key="recipients",
        amount_field="distributeAmount",
    )
    return await _submit(
        request,
        ids=ids,
        biz_seq_no=body.bizSeqNo,
        business_action="DISTRIBUTE",
        biz_type="DST",
        business_scope="bank_distribute",
        wedap_method="distribute_to_users",
        amount=amount,
        currency=body.currencyCode,
        wedap_payload=payload,
    )


@router.get("/status")
async def query_status(
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
    biz_seq_no: str = Query(..., alias="bizSeqNo"),
) -> dict[str, Any]:
    """查询本地 order 状态 + wedap 实时状态（wedap 不可用时降级为 unavailable=True）。"""
    factory = request.app.state.session_factory
    async with factory() as session:
        row = await session.scalar(
            select(BankTxnOrder).where(
                BankTxnOrder.tenant_id == ids["tenant_id"],
                BankTxnOrder.biz_seq_no == biz_seq_no,
            )
        )
    if row is None:
        raise HTTPException(
            404,
            detail={"code": "GW_404_ORDER", "message": f"order not found: {biz_seq_no}"},
        )

    # 查询 wedap 实时状态；失败降级，不向外抛 500
    try:
        wedap_data: dict[str, Any] = await request.app.state.wedap.query_funds_status(
            tenant_id=ids["tenant_id"],
            request_id=ids["request_id"],
            biz_seq_no=biz_seq_no,
            biz_type=row.biz_type,
        )
    except (httpx.TimeoutException, httpx.TransportError):
        wedap_data = {"unavailable": True, "reason": "timeout"}
    except httpx.HTTPStatusError:
        wedap_data = {"unavailable": True, "reason": "http_error"}
    except WedapError as exc:
        reason = "no_status_api" if exc.code == "UNSUPPORTED" else "wedap_error"
        wedap_data = {"unavailable": True, "reason": reason}

    result: dict[str, Any] = {
        "bizSeqNo": row.biz_seq_no,
        "orderStatus": row.status,
        "wedap": wedap_data,
    }
    return ok(result, trace_id=ids["trace_id"])
