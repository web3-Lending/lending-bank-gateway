"""北向银行资金 API：collect-from-users + distribute-to-users + status。"""

from decimal import Decimal
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
from app.services.account_guard import assert_platform_account_allowed
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
    # wedap 必填且 ≤20（W2 实测硬限）；gateway 落库供状态回查（提交值==查询值），
    # 缺失/超长在入口显式拒绝——静默截断会造成「回查值 != 提交值」永久查不到（codex P1）。
    transType: str = Field(min_length=1, max_length=20)
    txnAmount: str | None = None
    totalAmount: str | None = None


class DistributeRequest(BaseModel):
    # 分发薄透传：wedap body = 顶层 currencyCode + recipients[].distributeAmount（无 totalAmount）。
    # recipients 各项的 userId/custAccountNo/bankAccountNo/vaultId 等经 extra=allow 原样透传。
    model_config = ConfigDict(extra="allow")
    bizSeqNo: str
    currencyCode: str
    transType: str = Field(min_length=1, max_length=20)
    recipients: list[dict[str, Any]] | None = None
    """显式 null 视同缺省，契约 C 下 wedap 可选字段缺省=null 语义等价。"""


class RefundRequest(BaseModel):
    # 退款薄透传（对接文档 v0.3.0 §4.7）：gateway 只取记账/幂等所需最少键；
    # bankAccountNo/custAccountNo/subaccountSerialNo/postscript 等经 extra=allow 原样透传。
    # 累计退款 ≤ 原单金额等业务校验在 wedap 侧（FOR UPDATE 串行防并发超额），gateway 不复刻。
    model_config = ConfigDict(extra="allow")
    bizSeqNo: str
    currencyCode: str
    transType: str = Field(min_length=1, max_length=20)
    refundAmount: str
    oriBizSeqNo: str
    """关联被退款的原归集单（清算超收退款只对未分发归集单做，业务约束在调用方）。"""


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
    # 账户守门人（钱能去哪，与 S2S 的「谁能调」正交）：三个资金端点
    # （collect/distribute/refund）都经本 helper，一处收口全覆盖；enforce 拒绝
    # 发生在 submit_order 之前——不写 order、不占幂等、不调 wedap（fail-closed）。
    await assert_platform_account_allowed(
        request.app.state.session_factory,
        wedap_payload.get("bankAccountNo"),
        tenant_id=ids["tenant_id"],
        business_scope=business_scope,
        currency=currency,
        caller=ids["caller_service"],
        trace_id=ids["trace_id"],
        mode=request.app.state.settings.account_guard_mode,
    )
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
    # 金额优先 wedap 扁平 txnAmount，过渡回退 totalAmount；空串/缺失视为缺。
    # 归集单用户无明细，不做 sum 校验。
    raw_amount = body.txnAmount or body.totalAmount
    if not raw_amount:
        raise HTTPException(
            400,
            detail={"code": "GW_400_VALIDATION", "message": "missing txnAmount"},
        )
    amount = parse_amount(raw_amount, body.currencyCode)
    # txnAmount 在场时 totalAmount 属旧形态噪声字段，不透传 wedap（避免注入伪字段 + 幂等漂移）
    if body.txnAmount and "totalAmount" in payload:
        payload.pop("totalAmount")
    return await _submit(
        request,
        ids=ids,
        biz_seq_no=body.bizSeqNo,
        business_action="COLLECT",
        biz_type="COLL",
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
            parse_amount(str(r["distributeAmount"]), body.currencyCode)
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
        biz_type="DIST",
        business_scope="bank_distribute",
        wedap_method="distribute_to_users",
        amount=amount,
        currency=body.currencyCode,
        wedap_payload=payload,
    )


@router.post("/refunds")
async def refund_to_user(
    body: RefundRequest,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    """退款北向端点（S5.6 拍板 2026-07-14：只补 refund，freeze/unfreeze 不做）。

    清算资金链口径（liquidation 调研）只用 collect + refund；退款经 gateway 落
    bank_txn_order（biz_type=RFND）保台账/幂等/对账覆盖，不允许上游直调 wedap 绕网关。
    """
    assert_idempotency_key_matches(request, body.bizSeqNo)
    amount = parse_amount(body.refundAmount, body.currencyCode)
    # 全额退款护栏（flag 默认关，wedap 冲正 4.8 落地后启用）：全额退款应走冲正原交易，
    # refund 仅部分退款（用户拍板 2026-07-15）。原单以 (tenant, oriBizSeqNo) 查本地台账；
    # 查不到不拦（可能非本 gateway 出单，业务校验交 wedap）。
    if request.app.state.settings.refund_full_amount_guard:
        async with request.app.state.session_factory() as session:
            ori = await session.scalar(
                select(BankTxnOrder).where(
                    BankTxnOrder.tenant_id == ids["tenant_id"],
                    BankTxnOrder.biz_seq_no == body.oriBizSeqNo,
                )
            )
        if ori is not None and amount == ori.amount:
            raise HTTPException(
                422,
                detail={
                    "code": "GW_422_FULL_REFUND_USE_REVERSAL",
                    "message": "full-amount refund must use reversal; refund is partial-only",
                },
            )
    payload = body.model_dump(mode="json", exclude_none=True)
    return await _submit(
        request,
        ids=ids,
        biz_seq_no=body.bizSeqNo,
        business_action="REFUND",
        biz_type="RFND",
        business_scope="bank_refund",
        wedap_method="refund",
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

    # 查询 wedap 实时状态（通用 /transactions/status，真实现；旧 per-type 桩已弃用——
    # 桩对假单也回 SUCCESS，该视图曾不可信）；失败降级，不向外抛 500
    if not row.trans_type or not row.ori_req_date:
        # 0020 前存量单缺回查供参：不打 wedap，以 orderStatus（本地权威态）为准
        wedap_data: dict[str, Any] = {
            "unavailable": True,
            "reason": "missing_trans_type",
            "note": "0020 前存量单缺 trans_type/ori_req_date，无法通用回查；以 orderStatus 为准",
        }
    else:
        try:
            wedap_data = await request.app.state.wedap.query_transaction_status(
                tenant_id=ids["tenant_id"],
                request_id=ids["request_id"],
                ori_biz_seq_no=biz_seq_no,
                trans_type=row.trans_type,
                ori_req_date=row.ori_req_date,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            wedap_data = {"unavailable": True, "reason": "timeout"}
        except httpx.HTTPStatusError:
            wedap_data = {"unavailable": True, "reason": "http_error"}
        except WedapError:
            wedap_data = {"unavailable": True, "reason": "wedap_error"}

    result: dict[str, Any] = {
        "bizSeqNo": row.biz_seq_no,
        "orderStatus": row.status,
        "wedap": wedap_data,
    }
    return ok(result, trace_id=ids["trace_id"])
