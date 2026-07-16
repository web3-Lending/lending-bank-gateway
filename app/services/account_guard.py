"""平台账户守门人：collect/distribute 白名单校验（FU-GW-ACCOUNT-GUARD-20260616-001）。

「谁能调」由 S2S 层管；本模块管「钱能去哪」——payload 顶层 ``bankAccountNo``
（平台中转户）必须命中 ``platform_bank_account`` 白名单：``(tenant_id, account_no)``
存在、``status=active``、``business_scope ∈ allowed_scopes``、币种一致或未限定。

三态 ``GW_ACCOUNT_GUARD_MODE``：off=不校验（默认）；observe=不拒、结构化日志 +
审计记录「会被拒」项（上线前抓全白名单）；enforce=403 拒绝，不写 order、不调
wedap（fail-closed，含空名单租户——空白名单 = 配置缺失，不是豁免）。

设计取舍（2026-07-16 评审拍板）：
- 直查库不缓存：表 < 百行、主键点查 < 1ms、gateway QPS 低——缓存属过早优化（D2）。
- 无 Prometheus 依赖：gateway 无 metrics 基建，reject 以结构化 WARNING 日志承载
  （stdout JSON → 平台日志聚合可统计），observe 期据此判断白名单是否漏配。
- 只拒不改写：比较前仅 strip 首尾空白，不做大小写归一——保持「禁 normalize、
  全链路同值」原则；账号大小写按字面匹配，白名单登记时以银行户口原文为准。

边界与语义声明（2026-07-16 codex 对抗审后固化）：
- 顶层 bankAccountNo = 平台中转户，实证于上游 lending-lifecycel
  admin-backend/app/domain_ops/services/bank_fund.py:96（collect）/:295（distribute
  顶层，与 recipients[] 用户收款户分层并存）——守门对象即该字段；
  recipients[].bankAccountNo（用户侧）为二期范围。
- loans 域（app/api/v1/loans.py 组合交易）不经本守门：其契约无顶层平台账户字段，
  账户策略需按该域契约单独建模（FU-GW-ACCOUNT-GUARD 跟踪），不机械复用本校验。
- 撤权语义：guard 查询是授权线性化点——admin PATCH disable 只保证「其后开始的
  请求」被拒；已过查询点的在途请求会继续完成。紧急止付须配合 wedap 侧拦截，
  本守门不提供在途撤回。
"""

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.platform_account import PlatformBankAccount
from app.services.audit import write_audit

logger = logging.getLogger(__name__)

REASON_MISSING = "account_missing"
REASON_NOT_REGISTERED = "account_not_registered"
REASON_DISABLED = "account_disabled"
REASON_SCOPE = "scope_not_allowed"
REASON_CURRENCY = "currency_mismatch"


@dataclass(frozen=True)
class GuardVerdict:
    ok: bool
    reason: str | None = None


def _mask(acct: str) -> str:
    """日志脱敏：仅保留末 4 位（审计表记完整账号，日志不记）。"""
    return f"***{acct[-4:]}" if len(acct) >= 4 else "***"


async def _evaluate(
    session: AsyncSession,
    *,
    tenant_id: str,
    account_no: str,
    business_scope: str,
    currency: str | None,
) -> GuardVerdict:
    if not account_no:
        return GuardVerdict(False, REASON_MISSING)
    # await 保持单行：py3.12 + coverage 对多行 await 之后的行有假 miss
    # （memory: coverage-312-multiline-await-line-miss），结构绕行而非补无意义用例。
    stmt = select(PlatformBankAccount).where(
        PlatformBankAccount.tenant_id == tenant_id,
        PlatformBankAccount.account_no == account_no,
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return GuardVerdict(False, REASON_NOT_REGISTERED)
    if row.status != "active":
        return GuardVerdict(False, REASON_DISABLED)
    scopes = {s.strip() for s in row.allowed_scopes.split(",") if s.strip()}
    if business_scope not in scopes:
        return GuardVerdict(False, REASON_SCOPE)
    # 白名单限定了币种时严格相等；请求缺币种也按 mismatch 拒（fail-closed）。
    if row.currency and row.currency != (currency or ""):
        return GuardVerdict(False, REASON_CURRENCY)
    return GuardVerdict(True)


async def assert_platform_account_allowed(
    session_factory: async_sessionmaker[AsyncSession],
    account_no: object | None,
    *,
    tenant_id: str,
    business_scope: str,
    currency: str | None,
    caller: str,
    trace_id: str,
    mode: str,
) -> None:
    """collect/distribute 平台账户校验入口（endpoint 层，早于 _submit）。

    mode=off 直接返回；observe/enforce 下查白名单，非法则结构化日志 + 审计落库，
    enforce 再抛 403（``GW_403_ACCOUNT_NOT_ALLOWED``）。审计写在独立事务：
    reject 路径不进 _submit，无既有事务可挂。
    """
    if mode == "off":
        return
    # extra=allow 透传下 bankAccountNo 可能是任意 JSON 标量：非空一律按字面 str 比较
    # （JSON number 户口号 → 数字串），None/空白 → REASON_MISSING。
    normalized = str(account_no).strip() if account_no is not None else ""
    eval_kwargs: dict[str, Any] = {
        "tenant_id": tenant_id,
        "business_scope": business_scope,
        "currency": currency,
    }
    try:
        verdict = await _check_and_record(
            session_factory, normalized, mode=mode, caller=caller, trace_id=trace_id, **eval_kwargs
        )
    except Exception:
        # 守卫基础设施故障（DB 不可达 / 审计写失败）语义分档（codex P1）：
        # observe = 纯观察，绝不因守卫自身故障阻断业务——记 ERROR 放行；
        # enforce = fail-closed，无法核验就不放钱，向上抛（500）。
        if mode == "observe":
            logger.exception(
                "account_guard: infra failure in observe mode, allowing business "
                "tenant=%s scope=%s trace=%s",
                tenant_id,
                business_scope,
                trace_id,
            )
            return
        raise
    if verdict.ok:
        return
    if mode == "observe":
        return
    raise HTTPException(
        403,
        detail={
            "code": "GW_403_ACCOUNT_NOT_ALLOWED",
            "message": "platform account not allowed",
            "reason": verdict.reason,
        },
    )


async def _check_and_record(
    session_factory: async_sessionmaker[AsyncSession],
    normalized: str,
    *,
    tenant_id: str,
    business_scope: str,
    currency: str | None,
    caller: str,
    trace_id: str,
    mode: str,
) -> GuardVerdict:
    """查白名单；未命中则结构化日志 + 审计落库（独立事务），返回判定。"""
    eval_kwargs: dict[str, Any] = {
        "tenant_id": tenant_id,
        "account_no": normalized,
        "business_scope": business_scope,
        "currency": currency,
    }
    async with session_factory() as session:
        async with session.begin():
            verdict = await _evaluate(session, **eval_kwargs)
            if verdict.ok:
                return verdict
            logger.warning(
                "account_guard: rejected tenant=%s scope=%s caller=%s acct=%s "
                "reason=%s mode=%s trace=%s",
                tenant_id,
                business_scope,
                caller,
                _mask(normalized),
                verdict.reason,
                mode,
                trace_id,
            )
            audit_kwargs: dict[str, Any] = {
                "tenant_id": tenant_id,
                "actor": f"svc:{caller}",
                "action": "account_guard.observe" if mode == "observe" else "account_guard.reject",
                "entity": "platform_account",
                "payload": {
                    "account_no": normalized,
                    "business_scope": business_scope,
                    "currency": currency,
                    "reason": verdict.reason,
                    "trace_id": trace_id,
                },
            }
            await write_audit(session, **audit_kwargs)
    return verdict
