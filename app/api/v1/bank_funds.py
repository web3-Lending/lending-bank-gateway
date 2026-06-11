"""北向银行资金 API：collect-from-users + distribute-to-users。"""

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import parse_amount, require_headers
from app.core.envelope import ok
from app.services.idempotency import IdempotencyConflict
from app.services.submit import SubmitRequest, submit_order

router = APIRouter(prefix="/api/v1/bank-funds", tags=["bank-funds"])


# ── Pydantic request schemas ───────────────────────────────────────────────────


class BankFundsRequest(BaseModel):
    bizSeqNo: str
    totalAmount: str
    currencyCode: str
    userList: list[dict[str, Any]] = Field(default_factory=list)


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
    body: BankFundsRequest,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    amount = parse_amount(body.totalAmount)
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
        wedap_payload=body.model_dump(),
    )


@router.post("/distribute-to-users")
async def distribute_to_users(
    body: BankFundsRequest,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    amount = parse_amount(body.totalAmount)
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
        wedap_payload=body.model_dump(),
    )
