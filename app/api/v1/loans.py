"""北向贷款交易 API：p2p-disbursements + p2p-repayments（含还款专用状态查询）。"""

from decimal import Decimal
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import (
    assert_idempotency_key_matches,
    bank_req_date,
    parse_amount,
    require_headers,
    validate_detail_consistency,
)
from app.clients.wedap import WedapError
from app.core.envelope import ok
from app.models.txn import BankTxnOrder
from app.services.idempotency import IdempotencyConflict
from app.services.submit import SubmitRequest, submit_order

router = APIRouter(prefix="/api/v1/loans", tags=["loans"])


# ── Pydantic request schemas ───────────────────────────────────────────────────


class DisbursementInfo(BaseModel):
    # 契约 C 薄透传：最少键取金额/币种，余下 wedap 字段经 extra=allow 透传。
    model_config = ConfigDict(extra="allow")
    txnAmount: str
    currencyCode: str
    userId: str = ""
    userName: str = ""


class RepaymentInfo(BaseModel):
    # wedap 必填字段（principalAmount/interestAmount/userId 等）嵌在此层，
    # 嵌套层须同样 extra=allow（只给顶层不够），否则被 pydantic 静默丢。
    model_config = ConfigDict(extra="allow")
    txnAmount: str
    currencyCode: str


class P2PDisbursementRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    bizSeqNo: str
    channelId: str = ""
    # wedap 必填且 ≤32（2026-07-24 wedap 定稿字典后上限 20→32，BANK_FUND_COLLECT_CLEARING=26）；
    # 落库供状态回查（提交值==查询值），缺失/超长入口显式拒绝（codex P1）
    transType: str = Field(min_length=1, max_length=32)
    disbursementInfo: DisbursementInfo
    lenders: list[dict[str, Any]] | None = None
    """Schema allows absent/null so validate_detail_consistency can emit a uniform GW_400
    "missing lenders" (rather than a pydantic 422, keeping the error envelope consistent);
    wedap DisbursementAdapterRequest.lenders is @NotEmpty, so the endpoint passes
    require_detail=True to enforce non-empty."""


class P2PRepaymentRequest(BaseModel):
    # 顶层 extra=allow 透传 lenders[] 等字段（不显式声明 lenders，靠 extra 捕获并进 model_dump）。
    model_config = ConfigDict(extra="allow")
    bizSeqNo: str
    transType: str = Field(min_length=1, max_length=32)
    repaymentInfo: RepaymentInfo


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
    repayment_contract: bool = False,
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
                ori_req_date=bank_req_date(request),
                repayment_contract=repayment_contract,
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


@router.post("/p2p-disbursements")
async def p2p_disbursement(
    body: P2PDisbursementRequest,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    assert_idempotency_key_matches(request, body.bizSeqNo)
    amount = parse_amount(body.disbursementInfo.txnAmount, body.disbursementInfo.currencyCode)
    payload = body.model_dump(mode="json", exclude_none=True)
    validate_detail_consistency(
        payload,
        total=amount,
        currency=body.disbursementInfo.currencyCode,
        detail_key="lenders",
        amount_field="lendAmount",
        require_detail=True,  # wedap DisbursementAdapterRequest.lenders is @NotEmpty
    )
    return await _submit(
        request,
        ids=ids,
        biz_seq_no=body.bizSeqNo,
        business_action="DISBURSE",
        biz_type="DISB",
        business_scope="p2p_disburse",
        wedap_method="submit_disbursement",
        amount=amount,
        currency=body.disbursementInfo.currencyCode,
        wedap_payload=payload,
    )


@router.post("/p2p-repayments")
async def p2p_repayment(
    body: P2PRepaymentRequest,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    assert_idempotency_key_matches(request, body.bizSeqNo)
    amount = parse_amount(body.repaymentInfo.txnAmount, body.repaymentInfo.currencyCode)
    payload = body.model_dump(mode="json", exclude_none=True)
    # 含费还款口径（权威 wedap 契约 :169 + 上游 admin-backend _align_amounts_for_baffle）：
    # repaymentInfo.txnAmount = 含费总额 = Σlenders.txnAmount + ΣfeeDeductions.feeAmount。
    # lenders.txnAmount = 出借人应收本息（wedap 真实字段，按占比拆分）；feeDeductions.feeAmount
    # = 转银行的费用/罚息（字段名 feeAmount，非 amount）。费用明细缺失时退化为纯本息 sum 校验。
    validate_detail_consistency(
        payload,
        total=amount,
        currency=body.repaymentInfo.currencyCode,
        detail_key="lenders",
        amount_field="txnAmount",
        fee_detail_key="feeDeductions",
        fee_amount_field="feeAmount",
        require_detail=True,  # wedap RepaymentAdapterRequest.lenders is @NotEmpty
    )
    return await _submit(
        request,
        ids=ids,
        biz_seq_no=body.bizSeqNo,
        business_action="REPAY",
        biz_type="RPMT",
        business_scope="p2p_repay",
        wedap_method="submit_repayment",
        amount=amount,
        currency=body.repaymentInfo.currencyCode,
        wedap_payload=payload,
        # 还款走 DTC 组合交易引擎的专属受理响应契约（v0.6.1 §4.2）：status/detailStatus/
        # debtSettled/globalTxId，无 txnStatus。仅本端点置 True。
        repayment_contract=True,
    )


@router.get("/p2p-repayments/{biz_seq_no}/status")
async def query_repayment_status(
    biz_seq_no: str,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    """还款专用状态查询（对接文档 v0.6.1 §4.2）：本地 order 状态 + wedap 逐笔明细。

    为什么不复用 `/bank-funds/status`（5.5 通用查询）：5.5 自 v0.6.0 起虽能查还款**顶层
    状态**，但 `debtSettled`（Lending 核销债务的唯一依据）与 `steps[]`（逐笔与银行对账）
    **只有本专用接口提供**。受理响应为 PROCESSING 时按本接口轮询至终态。

    本地 order 不存在 → 404 且不打 wedap：存在性判据在本方台账，不拿外部系统当权威
    （亦防 bizSeqNo 探测）。wedap 不可用 → 降级 `unavailable`，本地 orderStatus 仍可读。
    """
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

    try:
        wedap_data: dict[str, Any] = await request.app.state.wedap.query_repayment_status(
            tenant_id=ids["tenant_id"],
            request_id=ids["request_id"],
            biz_seq_no=biz_seq_no,
        )
    except (httpx.TimeoutException, httpx.TransportError):
        wedap_data = {"unavailable": True, "reason": "timeout"}
    except httpx.HTTPStatusError:
        wedap_data = {"unavailable": True, "reason": "http_error"}
    except WedapError:
        wedap_data = {"unavailable": True, "reason": "wedap_error"}

    return ok(
        {"bizSeqNo": row.biz_seq_no, "orderStatus": row.status, "wedap": wedap_data},
        trace_id=ids["trace_id"],
    )
