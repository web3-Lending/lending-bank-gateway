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
from app.services.reversal import submit_reversal
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


class ReversalRequest(BaseModel):
    # 通用冲正薄透传（对接文档 Public.md §4.4.2）：全额冲正原归集单，gateway 只取记账/幂等
    # /原单翻转所需最少键；channelId/oriRequestId/bankAccountNo 等经 extra=allow 原样透传。
    # 三方防冲错校验（oriTxnAmount/currencyCode == 本地原单 == BANK-313）
    # 在 wedap 侧，gateway 不复刻。
    model_config = ConfigDict(extra="allow")
    bizSeqNo: str
    currencyCode: str
    # transType=原交易类型（一期归集类），wedap 按 (oriBizSeqNo, transType) 消歧；≤20 落库供回查。
    transType: str = Field(min_length=1, max_length=20)
    oriBizSeqNo: str
    """被冲正的原交易流水号；本地查得到则同步翻 REVERSED，查不到不拦（同 refund）。"""
    oriTxnAmount: str
    """原交易金额（wedap 防冲错校验用，非可调冲正金额）。"""


# ── Response schema（提交响应最小字段契约 · 2026-07-22）──────────────────────────


class SubmitAck(BaseModel):
    """写原语提交响应 data 段最小字段契约（collect/distribute/refund/reversal 共用）。

    - ``txnStatus``：wedap 同步语义（SUCCESS 已同步终态 / PROCESSING 异步在途 /
      FAILED / RESULT_UNKNOWN）
    - ``orderStatus``：gateway 台账 :class:`~app.domain.states.OrderStatus`
      （ACCEPTED/SUBMITTED/SUCCEEDED/FAILED/RESULT_UNKNOWN/...），受理 vs 落账
      以此字段区分；权威终态仍以 ``GET /bank-funds/status`` 为准。
      缺失的两种情形：契约上线前受理单的幂等重放（first_response 冻结不回填）；
      in-flight 重放（``inFlight=true``，零查询路径不读 order 行）——均应查单。
    - 未设置字段序列化省略（端点挂 ``response_model_exclude_unset``）：线格式对既有
      调用方纯增量（不出现 ``errorCode: null`` 噪声）。不能用 model_serializer 丢 None
      ——wrap serializer 的 dict 返回注解会把 openapi schema 打回 additionalProperties。
    """

    bizSeqNo: str
    txnStatus: str
    orderStatus: str | None = None
    errorCode: str | None = None
    errorMsg: str | None = None
    inFlight: bool | None = None


class SubmitAckEnvelope(BaseModel):
    """写原语提交 200 响应统一 envelope（:func:`app.core.envelope.ok` 的类型化形态）。"""

    success: bool
    data: SubmitAck
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


@router.post(
    "/collect-from-users", response_model=SubmitAckEnvelope, response_model_exclude_unset=True
)
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


@router.post(
    "/distribute-to-users", response_model=SubmitAckEnvelope, response_model_exclude_unset=True
)
async def distribute_to_users(
    body: DistributeRequest,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    assert_idempotency_key_matches(request, body.bizSeqNo)
    payload = body.model_dump(mode="json", exclude_none=True)
    # 分发 wedap 契约无顶层总额：本地账本/幂等金额 = Σ recipients[].distributeAmount。
    # distributeAmount 必填（2026-07-17 决策①方案乙）：「缺省=自动分配」在 wedap 契约中
    # 不存在（旧注释是 e480f55 自产防御话术），真实调用方 lifecycel 恒带金额；缺省会让
    # sum=0 落 bank_txn_order 台账锚点并污染 refund 全额护栏基准（R3-DST-07171057 实锤）。
    recipients = body.recipients or []
    if not recipients:
        raise HTTPException(
            400,
            detail={
                "code": "GW_400_VALIDATION",
                "message": "recipients is required for distribute",
            },
        )
    for idx, r in enumerate(recipients):
        if not isinstance(r, dict) or r.get("distributeAmount") is None:
            raise HTTPException(
                400,
                detail={
                    "code": "GW_400_VALIDATION",
                    "message": f"recipients[{idx}].distributeAmount is required",
                },
            )
    amount = sum(
        (parse_amount(str(r["distributeAmount"]), body.currencyCode) for r in recipients),
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


@router.post("/refunds", response_model=SubmitAckEnvelope, response_model_exclude_unset=True)
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
    # 全额退款护栏（硬规则 · 用户拍板 2026-07-22：全额退款一律走冲正，不再可配置）：
    # 全额退款应走冲正原交易 /bank-funds/reversals，refund 仅部分退款。原单以
    # (tenant, oriBizSeqNo) 查本地台账；查不到不拦（可能非本 gateway 出单，业务校验交 wedap）。
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


@router.post("/reversals", response_model=SubmitAckEnvelope, response_model_exclude_unset=True)
async def reverse_transaction(
    body: ReversalRequest,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    """通用冲正北向端点（对接 wedap Public.md §4.4.2，全额冲正原归集单）。

    落 RVSL 单（biz_type=RVSL）→ 同步调 wedap 通用冲正 → HTTP 200 即指令受理成功
    （RVSL 单 SUCCEEDED），同一事务把本地原单 SUCCEEDED→REVERSED（查不到不拦）。
    通用冲正同步无回调，不走既有 callback 摄取链。仅全额冲正，部分退款走 /refunds。
    """
    assert_idempotency_key_matches(request, body.bizSeqNo)
    amount = parse_amount(body.oriTxnAmount, body.currencyCode)
    payload = body.model_dump(mode="json", exclude_none=True)
    try:
        result = await submit_reversal(
            request.app.state.session_factory,
            wedap_reverse=request.app.state.wedap.reverse,
            req=SubmitRequest(
                tenant_id=ids["tenant_id"],
                biz_seq_no=body.bizSeqNo,
                business_action="REVERSE",
                biz_type="RVSL",
                amount=amount,
                currency=body.currencyCode,
                caller_service=ids["caller_service"],
                request_id=ids["request_id"],
                business_scope="bank_reversal",
                wedap_payload=payload,
                ori_req_date=bank_req_date(request),
            ),
            ori_biz_seq_no=body.oriBizSeqNo,
        )
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "GW_400_VALIDATION", "message": str(exc)}) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(
            409, detail={"code": "GW_409_IDEMPOTENCY", "message": f"idempotency conflict: {exc}"}
        ) from exc
    return ok(result, trace_id=ids["trace_id"])


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
