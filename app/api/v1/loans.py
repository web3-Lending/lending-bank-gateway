"""北向贷款交易 API：p2p-disbursements + p2p-repayments（含还款专用状态查询）。"""

from decimal import Decimal
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import (
    assert_idempotency_key_matches,
    assert_wedap_required,
    bank_req_date,
    parse_amount,
    require_headers,
    validate_detail_consistency,
)
from app.api.v1.bank_funds import SubmitAck, SubmitAckEnvelope
from app.clients.wedap import WedapError
from app.core.envelope import ok
from app.domain.wedap_contract import (
    DISBURSEMENT_INFO_REQUIRED,
    DISBURSEMENT_LENDER_REQUIRED,
    DISBURSEMENT_REQUIRED,
    REPAYMENT_INFO_REQUIRED,
    REPAYMENT_LENDER_REQUIRED,
    REPAYMENT_REQUIRED,
)
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
    # loanNo 是 wedap 必填（见 wedap_contract.REPAYMENT_REQUIRED 的依据注释），但**显式声明为
    # 可选**：强制由 assert_wedap_required 统一执行，缺失才与其它必填项一样出 GW_400_VALIDATION
    # 并一次列全；若在此标必填，pydantic 会抢先返 422 且错误形态与同批字段不一致。
    #
    # 声明它而不靠 extra 静默透传：字段必须在 openapi 里可见，否则调用方无从知道要传
    # （debtSettled 至今未被上游接入正是「只靠口头传达」的成因，见 RepaymentAck 注释）。
    loanNo: str | None = Field(
        default=None,
        # schema 上是 optional（强制交给 assert_wedap_required 以统一错误形态），故必填语义
        # 只能靠 description 向新调用方表达——否则规格读起来像"可不传"，与实际行为相反
        # （codex 复核 2026-08-26）。
        description=(
            "借据单号。wedap 侧自 2026-07-23 起无条件必填（loan_repayment_txn.loan_no "
            "NOT NULL，灰度开关同迁移移除）；缺失时本网关返 400 GW_400_VALIDATION，"
            "不下发 wedap。schema 上标 optional 仅为统一错误形态，业务上必传。"
        ),
    )
    repaymentInfo: RepaymentInfo


# ── 响应契约（openapi 可见） ────────────────────────────────────────────────────
# 复用 bank_funds 的 SubmitAck 而非另起一套：写原语提交响应对调用方是同一形态契约，
# 分叉会让「查 openapi 就知道能拿到什么」失效。还款额外带 DTC 三字段，故子类扩展。


class RepaymentAck(SubmitAck):
    """还款受理响应 data 段（对接文档 v0.6.1 §4.2，SubmitAck + DTC 三字段）。

    这三个字段是 v0.5.0 随组合交易引擎新增的，**必须在 openapi 里可见**——否则调用方
    无从知道 ``debtSettled`` 存在，只能靠口头传达（上游至今未接该字段正是此成因）。

    - ``debtSettled``：契约指定的**债务核销唯一依据**，仅全部资金步骤成功时为 true
    - ``globalTxId``：wedap 组合交易实例号，报障/对账时提供给 wedap
    - ``detailStatus``：细分状态，供排查参考；**状态机仍以 txnStatus 为准**
    逐笔 ``steps[]`` 与 ``strandedAmount`` 不在受理响应，须查本模块的专用状态查询端点。
    """

    debtSettled: bool | None = None
    globalTxId: str | None = None
    detailStatus: str | None = None


class RepaymentAckEnvelope(BaseModel):
    """还款受理 200 响应统一 envelope。"""

    success: bool
    data: RepaymentAck
    error: dict[str, Any] | None
    trace_id: str


class RepaymentStatusData(BaseModel):
    """还款专用状态查询 200 响应 data 段。

    ``wedap`` 是**薄透传**段：正常时为 wedap 专用状态查询响应（含 ``debtSettled`` /
    ``strandedAmount`` / ``steps[]``），wedap 不可达时降级为
    ``{"unavailable": true, "reason": ...}``。逐笔 ``steps[]`` 的结构由 wedap 契约决定、
    本网关不复刻，故此段保持开放对象——但顶层三键必须在 openapi 里可见，否则调用方
    连「响应长什么样」都要靠口头传达（2026-08-10 独立评审 finding）。
    """

    bizSeqNo: str
    orderStatus: str
    wedap: dict[str, Any]


class RepaymentStatusEnvelope(BaseModel):
    """还款专用状态查询 200 响应统一 envelope。"""

    success: bool
    data: RepaymentStatusData
    error: dict[str, Any] | None
    trace_id: str


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


@router.post(
    "/p2p-disbursements", response_model=SubmitAckEnvelope, response_model_exclude_unset=True
)
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
    # wedap 必填集门禁（2026-08-11 实测，见 app/domain/wedap_contract）：这些字段原先全靠
    # extra=allow 透传、gateway 不看，被 wedap 400 拒时台账已留一条 FAILED 垃圾单
    # （submit_order 先落 ACCEPTED 再外呼）。
    assert_wedap_required(payload, DISBURSEMENT_REQUIRED)
    assert_wedap_required(
        payload.get("disbursementInfo") or {},
        DISBURSEMENT_INFO_REQUIRED,
        where="disbursementInfo",
    )
    # 不重复判型：上面 validate_detail_consistency 传的是**非空 total + main_strict 默认 True**，
    # 该组合下非 dict 的 lenders 项已在 _collect_detail_amounts 的 strict 分支被 400 挡掉。
    # 注意保障条件是这个组合、不是 require_detail=True 本身（total=None 会提前 return，
    # codex 复核 2026-08-11 纠正）；assert_wedap_required 另有非 dict fail-closed 兜底。
    for idx, lender in enumerate(payload.get("lenders") or []):
        assert_wedap_required(lender, DISBURSEMENT_LENDER_REQUIRED, where=f"lenders[{idx}]")
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


@router.post(
    "/p2p-repayments", response_model=RepaymentAckEnvelope, response_model_exclude_unset=True
)
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
    # wedap 必填集门禁（同放款）。principalAmount/interestAmount 是 @NotNull 的金额字段——
    # 0 合法（无息期还款），故判据是「存在且非 None」，不是「非零」。
    assert_wedap_required(payload, REPAYMENT_REQUIRED)
    assert_wedap_required(
        payload.get("repaymentInfo") or {}, REPAYMENT_INFO_REQUIRED, where="repaymentInfo"
    )
    # 同放款：非空 total + main_strict=True 的组合已让非 dict 的 lenders 项在前面 400；
    # assert_wedap_required 的非 dict fail-closed 是第二层兜底。
    for idx, lender in enumerate(payload.get("lenders") or []):
        assert_wedap_required(lender, REPAYMENT_LENDER_REQUIRED, where=f"lenders[{idx}]")
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


@router.get("/p2p-repayments/{biz_seq_no}/status", response_model=RepaymentStatusEnvelope)
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
