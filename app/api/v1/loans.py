"""北向贷款交易 API：p2p-disbursements + p2p-repayments。"""

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import assert_idempotency_key_matches, parse_amount, require_headers
from app.core.envelope import ok
from app.services.idempotency import IdempotencyConflict
from app.services.submit import SubmitRequest, submit_order

router = APIRouter(prefix="/api/v1/loans", tags=["loans"])


# ── Pydantic request schemas ───────────────────────────────────────────────────


class DisbursementInfo(BaseModel):
    txnAmount: str
    currencyCode: str
    userId: str = ""
    userName: str = ""


class RepaymentInfo(BaseModel):
    txnAmount: str
    currencyCode: str


class P2PDisbursementRequest(BaseModel):
    bizSeqNo: str
    channelId: str = ""
    transType: str = ""
    disbursementInfo: DisbursementInfo
    lenders: list[dict[str, Any]] = Field(default_factory=list)


class P2PRepaymentRequest(BaseModel):
    bizSeqNo: str
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


@router.post("/p2p-disbursements")
async def p2p_disbursement(
    body: P2PDisbursementRequest,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    assert_idempotency_key_matches(request, body.bizSeqNo)
    amount = parse_amount(body.disbursementInfo.txnAmount)
    return await _submit(
        request,
        ids=ids,
        biz_seq_no=body.bizSeqNo,
        business_action="DISBURSE",
        biz_type="DSB",
        business_scope="p2p_disburse",
        wedap_method="submit_disbursement",
        amount=amount,
        currency=body.disbursementInfo.currencyCode,
        wedap_payload=body.model_dump(mode="json"),
    )


@router.post("/p2p-repayments")
async def p2p_repayment(
    body: P2PRepaymentRequest,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    assert_idempotency_key_matches(request, body.bizSeqNo)
    amount = parse_amount(body.repaymentInfo.txnAmount)
    return await _submit(
        request,
        ids=ids,
        biz_seq_no=body.bizSeqNo,
        business_action="REPAY",
        biz_type="RPY",
        business_scope="p2p_repay",
        wedap_method="submit_repayment",
        amount=amount,
        currency=body.repaymentInfo.currencyCode,
        wedap_payload=body.model_dump(mode="json"),
    )
