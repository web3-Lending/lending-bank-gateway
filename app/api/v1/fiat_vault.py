from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_, select

from app.api.deps import require_headers
from app.core.envelope import ok
from app.models.txn import BankTxnLeg

router = APIRouter(prefix="/api/v1/fiat-vault", tags=["fiat-vault"])

_MAX_LIMIT = 1000


@router.get("/transactions")
async def vault_transactions(
    request: Request,
    accountNo: str,
    dateFrom: str,
    dateTo: str,
    limit: int = 500,
    hdr: dict[str, str] = Depends(require_headers),
) -> dict[str, object]:
    if not (dateFrom.isdigit() and len(dateFrom) == 8 and dateTo.isdigit() and len(dateTo) == 8):
        raise HTTPException(
            400, detail={"code": "GW_400_VALIDATION", "message": "dateFrom/dateTo must be yyyyMMdd"}
        )
    if dateFrom > dateTo:
        raise HTTPException(
            400, detail={"code": "GW_400_VALIDATION", "message": "dateFrom > dateTo"}
        )
    if not 1 <= limit <= _MAX_LIMIT:
        raise HTTPException(
            400, detail={"code": "GW_400_VALIDATION", "message": f"limit must be 1..{_MAX_LIMIT}"}
        )
    async with request.app.state.session_factory() as session:
        legs = (
            (
                await session.execute(
                    select(BankTxnLeg)
                    .where(
                        BankTxnLeg.tenant_id == hdr["tenant_id"],
                        or_(
                            BankTxnLeg.payee_account == accountNo,
                            BankTxnLeg.payer_account == accountNo,
                        ),
                        BankTxnLeg.txn_date >= dateFrom,
                        BankTxnLeg.txn_date <= dateTo,
                    )
                    .order_by(BankTxnLeg.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    items = [
        {
            "bizSeqNo": leg.biz_seq_no,
            "externalRef": leg.external_ref,
            "stepType": leg.step_type,
            "amount": str(leg.amount),
            "currency": leg.currency,
            "payer": leg.payer_account,
            "payee": leg.payee_account,
            "status": leg.status,
            "txnDate": leg.txn_date,
        }
        for leg in legs
    ]
    # totalAmount：双向流水总额（含 payer 侧与 payee 侧，即所有命中 leg 的 amount 之和）
    total = sum((Decimal(i["amount"]) for i in items), Decimal("0"))
    # inflowAmount：账户作为收款方（payee == accountNo）的流水之和
    inflow = sum(
        (Decimal(i["amount"]) for i in items if i["payee"] == accountNo),
        Decimal("0"),
    )
    # outflowAmount：账户作为付款方（payer == accountNo）的流水之和
    outflow = sum(
        (Decimal(i["amount"]) for i in items if i["payer"] == accountNo),
        Decimal("0"),
    )
    return ok(
        {
            "items": items,
            "aggregate": {
                "count": len(items),
                "totalAmount": f"{total:.4f}",
                "inflowAmount": f"{inflow:.4f}",
                "outflowAmount": f"{outflow:.4f}",
            },
        },
        trace_id=hdr["trace_id"],
    )
