import hashlib
import json
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency import IdempotencyRecord


class IdempotencyRejection(Exception):
    """幂等前置检查的拒绝基类：自带北向状态码 / 错误码 / 文案前缀。

    为什么把这三样挂在异常上：拒绝理由有两种（payload 不符 vs 幂等键状态冲突），
    北向码不同；而抛出点散在 ``check_or_register`` 与 ``register_and_accept_order``、
    捕获点散在三个 API helper。若让 API 层逐处 ``if isinstance(...)`` 分派，
    新增一种拒绝理由就要改三处、漏一处就掉进错误的码。
    """

    http_status: ClassVar[int]
    code: ClassVar[str]
    reason: ClassVar[str]


class IdempotencyPayloadMismatch(IdempotencyRejection):
    """同 key 不同 payload —— 北向必须回 422（v2.2 §9.1，API-HTTP-019）。

    §9.1：「同键、不同 payload：返回 HTTP 422，Problem
    ``type=https://api.wts.com/problems/idempotency-payload-mismatch/v1``；
    调用方必须停止重放并先查询原 operation。……服务端不得 dispatch。」

    **为什么从 409 改成 422（2026-08-31）**：409 是本仓自选口径，不是对外契约约束——
    北向契约 ``baffle/P2P_Lending_API_WEDAP.md`` 只写「接口具备幂等性」与「同一 bizSeqNo
    幂等重放返回同一值」，对 payload 不符该返什么码只字未提。§6 逐规则表把 API-HTTP-019
    的「例外审批人 / 例外到期」写成**不允许 / 不适用**，故没有限期例外可申请。
    改码前已逐个核对内部消费方（唯二调用方 lending-lifecycel 与 liquidation-backend）：
    两者对本网关的写端点都只有 ``400 <= status < 500`` 的统一分支，无任何按 409 的分支
    判断，故改码对它们是行为等价的（详见本次 commit message）。

    Problem 文档形态（``application/problem+json`` + ``LendingProblemV1``）不在本次范围：
    整仓 28 个 operation 的 error envelope 迁移是独立的破坏性批次，由
    ``app.core.openapi_contract.PROBLEM_COMPLIANCE_EXCEPTION_REF`` 跟踪。本次只纠正状态码，
    不单独给这一个错误造一个和其余 27 个不一致的响应形状。
    """

    http_status = 422
    code = "GW_422_IDEMPOTENCY_PAYLOAD_MISMATCH"
    reason = "idempotency payload mismatch"


class IdempotencyKeyStateConflict(IdempotencyRejection):
    """幂等键与台账状态冲突：order 行已存在、对应幂等行却缺失（人工补数 / 迁移脏状态）。

    **与 payload 不符是两回事，故码也不同**：这里根本读不到指纹，无从断言调用方
    换没换报文；能确定的只是「这个 bizSeqNo 已经被一张单占了」——这是 409 Conflict
    的本义。v2.2 §9.1 管的是「同键、不同 payload」，不覆盖本情形，把它一起并进 422
    等于对调用方谎称「你改了报文」，排障时会把人引向错误的方向。
    """

    http_status = 409
    code = "GW_409_IDEMPOTENCY"
    reason = "idempotency conflict"


class IdempotencyInFlight(Exception):
    """同 key 同 payload 但请求仍在处理中（first_response 为 None）——
    语义：相同请求正在被处理，调用方应返回 PROCESSING / 查询状态，禁止重新执行业务逻辑。
    """


def _json_default(obj: Any) -> str:
    """JSON 序列化兜底。

    调用方必须传 JSON-native 类型（Pydantic 用 .model_dump(mode="json")），
    金额统一字符串（如 "100.0000"）。

    Decimal 显式转 str 确保 Decimal("100.0000") 与 str "100.0000" hash 一致；
    其它不可序列化类型统一 str() 兜底，与 Python 内置 float 的 JSON 表示不同，
    故 float(100.0) 与 str "100.0000" 的 hash 不相同——调用方不应传 float 金额。
    """
    if isinstance(obj, Decimal):
        # 显式分支：Decimal("100.0000") → "100.0000"，与字符串入参 hash 一致
        return str(obj)
    return str(obj)


def payload_hash(payload: dict[str, Any]) -> str:
    """规范化报文指纹：key 排序 + 紧凑分隔符 + Decimal→str，取 SHA-256。

    **纳入范围：调用方送来的整份业务报文，一个字段都不剔。** 调用点传的是各端点
    ``body.model_dump(mode="json", exclude_none=True)`` 的产物，即 pydantic 声明字段
    与 ``extra=allow`` 透传字段的全集——金额、币种、账号、出借人明细、费用明细全在内。

    为什么默认全纳入（方向不对称，v2.2 §9.1 的失败模式分析）：
    - 多纳入一个字段，失败模式是**误拒一次重试**——调用方拿到明确的 422 和原因，
      看得见、可改正、钱没动。
    - 少纳入一个字段，失败模式是**把真的金额/收款人变更当成重复请求放过去**——
      回放旧结果并报成功，调用方以为新报文生效了，实际一分没动，且账面自洽、
      事后对账查不出来。
    故任何「这个字段应该不影响结果吧」的直觉都必须让位给「拿不准就纳入」。

    **不在指纹内的三类，及各自的理由**（要新增排除项，必须在此写清「为什么它不可能
    影响结果」，不许静默剔）：
    1. HTTP header 的关联 id（``X-Request-Id`` / ``X-Trace-Id``）：每次重试本就该换新值，
       纳入等于让任何一次正常重试都变成 422，指纹会彻底失效。
    2. 幂等三元组本身（``tenant_id`` / ``business_scope`` / ``idempotency_key``）：
       它们是行的查找键（``uq_idem_triple``），不同值定位到的是不同的幂等行，
       不存在「同键但这三项不同」的情形，放进指纹是重复计算。
    3. 服务端自己派生、调用方送不进来的值（如 ``SubmitRequest.ori_req_date``，由
       ``deps.bank_req_date`` 按银行时区当场生成）：它不表达调用方意图，且同一份报文
       跨银行午夜重试会天然变值 → 纳入同样会把正常重试打成 422。

    另有两处**形态归一**（不是排除，是让语义相同的两种写法算出同一指纹）：
    - ``exclude_none=True``：显式送 ``{"loanNo": null}`` 与整个字段缺省，下发给 wedap 的
      报文完全一致，故指纹也应一致。
    - 归集端点在 hash 前 ``payload.pop("totalAmount")``：该字段已不是本端点的合法入参，
      无论送不送都会被剪掉、绝不下发 wedap（见 ``bank_funds.collect_from_users``），
      故它不可能影响结果。
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def check_or_register(
    session: AsyncSession,
    *,
    tenant_id: str,
    business_scope: str,
    idempotency_key: str,
    method: str,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """幂等注册/查询。

    返回语义（三态）：
    - None：首次注册，调用方继续执行业务逻辑。
    - dict：已完成的重放，调用方直接返回 first_response。
    - raises IdempotencyInFlight：同 key 同 payload 但尚未记录响应，
      请求仍在处理中，调用方应返回 PROCESSING/查询状态，禁止重新执行业务逻辑。
    - raises IdempotencyPayloadMismatch：同 key 不同 payload，北向必须回 422
      （v2.2 §9.1），且不得 dispatch——本函数在任何外呼之前就把它抛出去。
    """
    h = payload_hash(payload)
    row = (
        await session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == tenant_id,
                IdempotencyRecord.business_scope == business_scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        try:
            async with session.begin_nested():
                session.add(
                    IdempotencyRecord(
                        tenant_id=tenant_id,
                        business_scope=business_scope,
                        idempotency_key=idempotency_key,
                        method=method,
                        path=path,
                        payload_hash=h,
                    )
                )
        except IntegrityError:
            # 并发同键：对手在 SELECT 与 INSERT 之间抢先插入；
            # FOR UPDATE 穿透 RR 快照，确保读到对手刚提交的行。
            row = (
                await session.execute(
                    select(IdempotencyRecord)
                    .where(
                        IdempotencyRecord.tenant_id == tenant_id,
                        IdempotencyRecord.business_scope == business_scope,
                        IdempotencyRecord.idempotency_key == idempotency_key,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:  # pragma: no cover - 理论不可达
                raise
        else:
            return None
    # 走到这里 row 非 None：比对 hash → 422 / in-flight / 重放。
    # payload_hash 列自建表迁移 0002 起就是 NOT NULL（本仓从无「指纹为空的历史行」
    # 这个宽容窗口需要处理），故直接比对即可，不需要 NULL 视同匹配的兼容分支。
    if row.payload_hash != h:
        raise IdempotencyPayloadMismatch(idempotency_key)
    if row.first_response is None:
        raise IdempotencyInFlight(idempotency_key)
    return row.first_response


async def record_response(
    session: AsyncSession,
    *,
    tenant_id: str,
    business_scope: str,
    idempotency_key: str,
    response: dict[str, Any],
    final_effect_id: str | None = None,
) -> None:
    row = (
        await session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == tenant_id,
                IdempotencyRecord.business_scope == business_scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(
            f"IdempotencyRecord not found: {tenant_id}/{business_scope}/{idempotency_key}"
        )
    row.first_response = response
    row.final_effect_id = final_effect_id
