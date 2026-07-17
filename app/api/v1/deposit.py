"""app/api/v1/deposit.py

deposit/users 查询透传端点：
- GET /api/v1/deposit/balances/total          → 余额汇总（审计 + 快照）
- GET /api/v1/deposit/accounts                → 账户列表（仅审计）
- GET /api/v1/deposit/account/detail          → 账户详情（仅审计，§5.3）
- GET /api/v1/deposit/transactions            → 交易流水（仅审计，§5.4）
- GET /api/v1/deposit/internal-accounts/info  → 内部户信息（审计 + 快照，§5.7）
- GET /api/v1/users/info                      → 用户信息（仅审计）

所有端点均写 QueryAudit；balances/total 与 internal-accounts/info 额外写 BalanceSnapshot。
脏余额数据（Decimal 解析失败）→ logger.warning 跳过该账户，不让查询整体失败。
WedapError / httpx 超时 → 502 GW_502_UPSTREAM。
"""

import datetime as dt
import hashlib
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import require_headers
from app.clients.wedap import WedapError
from app.core.envelope import ok
from app.models.query_audit import BalanceSnapshot, QueryAudit

logger = logging.getLogger(__name__)

router = APIRouter(tags=["deposit"])


async def _audited_passthrough(
    request: Request,
    *,
    endpoint: str,
    call: Callable[[], Awaitable[dict[str, Any]]],
    params: dict[str, str],
    hdr: dict[str, str],
) -> dict[str, Any]:
    """执行上游调用并写 QueryAudit 审计行。

    异常映射：
    - httpx.TimeoutException / httpx.TransportError → 502 GW_502_UPSTREAM (unreachable)
    - WedapError（上游业务 code != 200）→ 502 GW_502_UPSTREAM + "wedap code <code>"
    """
    try:
        data = await call()
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise HTTPException(
            502,
            detail={"code": "GW_502_UPSTREAM", "message": "wedap unreachable"},
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            502,
            detail={
                "code": "GW_502_UPSTREAM",
                "message": f"wedap http {exc.response.status_code}",
            },
        ) from exc
    except WedapError as exc:
        raise HTTPException(
            502,
            detail={"code": "GW_502_UPSTREAM", "message": f"wedap code {exc.code}"},
        ) from exc

    h = hashlib.sha256(repr(sorted(params.items())).encode()).hexdigest()
    async with request.app.state.session_factory() as session:
        async with session.begin():
            session.add(
                QueryAudit(
                    tenant_id=hdr["tenant_id"],
                    endpoint=endpoint,
                    params_hash=h,
                    trace_id=hdr["trace_id"],
                    caller_service=hdr["caller_service"],
                )
            )
    return data


@router.get("/api/v1/deposit/balances/total")
async def deposit_balance_total(
    request: Request,
    hdr: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    """余额汇总查询：原样透传所有 query params + 审计 + 余额快照。

    契约 C 薄透传：不再只挑 userId，按 users/info 同款全透传，wedap 必填的
    bizSeqNo/channelId 及四标识由调用方携带即原样转发，gateway 不剪裁。
    """
    params = dict(request.query_params)
    wedap = request.app.state.wedap
    data = await _audited_passthrough(
        request,
        endpoint="deposit/balances/total",
        params=params,
        hdr=hdr,
        call=lambda: wedap.get_deposit_balance_total(
            tenant_id=hdr["tenant_id"],
            request_id=hdr["request_id"],
            params=params,
        ),
    )
    now = dt.datetime.now(dt.UTC)
    try:
        async with request.app.state.session_factory() as session:
            async with session.begin():
                for acct in data.get("accounts", []):
                    raw_balance = acct.get("balance")
                    if acct.get("custAccountNo") is None or raw_balance is None:
                        # 缺 balance 不允许默认 0 落快照——0 会被当成真实余额锚点
                        # 造成假对平（与 internal-accounts/info 同口径）。
                        logger.warning(
                            "Skipping snapshot for account: missing custAccountNo/balance in %r",
                            acct,
                        )
                        continue
                    try:
                        balance = Decimal(str(raw_balance))
                    except InvalidOperation:
                        balance = None
                    # NaN/Infinity 能过 Decimal 构造但不是合法余额；adjusted()>=17
                    # 超 Numeric(21,4) 整数容量，落库会炸——一并跳过。
                    if balance is None or not balance.is_finite() or balance.adjusted() >= 17:
                        logger.warning(
                            "Skipping snapshot for account %s: invalid balance %r",
                            acct.get("custAccountNo"),
                            raw_balance,
                        )
                        continue
                    session.add(
                        BalanceSnapshot(
                            tenant_id=hdr["tenant_id"],
                            account_id=str(acct.get("custAccountNo")),
                            balance=balance,
                            currency=str(acct.get("currencyCode", "USD")),
                            source_endpoint="deposit/balances/total",
                            captured_at=now,
                        )
                    )
    except SQLAlchemyError:
        # 快照是旁路增强：审计与上游查询已成功后，DB 抖动/落库失败不允许把
        # 200 变 500（与 internal-accounts/info 同口径）。只告警，响应照常。
        logger.warning(
            "Snapshot write failed for balances/total; query response unaffected",
            exc_info=True,
        )
    return ok(data, trace_id=hdr["trace_id"])


@router.get("/api/v1/deposit/accounts")
async def deposit_accounts(
    request: Request,
    hdr: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    """账户列表查询：原样透传所有 query params（含 wedap 必填 bizSeqNo/channelId）+ 审计。"""
    params = dict(request.query_params)
    wedap = request.app.state.wedap
    data = await _audited_passthrough(
        request,
        endpoint="deposit/accounts",
        params=params,
        hdr=hdr,
        call=lambda: wedap.get_deposit_accounts(
            tenant_id=hdr["tenant_id"],
            request_id=hdr["request_id"],
            params=params,
        ),
    )
    return ok(data, trace_id=hdr["trace_id"])


@router.get("/api/v1/deposit/account/detail")
async def deposit_account_detail(
    request: Request,
    hdr: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    """账户详情查询（§5.3）：原样透传所有 query params（custAccountNo 等）+ 审计。"""
    params = dict(request.query_params)
    wedap = request.app.state.wedap
    data = await _audited_passthrough(
        request,
        endpoint="deposit/account/detail",
        params=params,
        hdr=hdr,
        call=lambda: wedap.get_deposit_account_detail(
            tenant_id=hdr["tenant_id"],
            request_id=hdr["request_id"],
            params=params,
        ),
    )
    return ok(data, trace_id=hdr["trace_id"])


@router.get("/api/v1/users/info")
async def users_info(
    request: Request,
    hdr: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    """用户信息查询：原样透传所有 query params + 审计。

    params 原样透传 wedap（契约 C）；参数白名单收紧候选项见 spec §11 协调清单。
    """
    params = dict(request.query_params)
    wedap = request.app.state.wedap
    data = await _audited_passthrough(
        request,
        endpoint="users/info",
        params=params,
        hdr=hdr,
        call=lambda: wedap.get_user_info(
            tenant_id=hdr["tenant_id"],
            request_id=hdr["request_id"],
            params=params,
        ),
    )
    return ok(data, trace_id=hdr["trace_id"])


@router.get("/api/v1/deposit/transactions")
async def deposit_transactions(
    request: Request,
    hdr: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    """交易流水查询（对接文档 v0.4.0 §5.4）：原样透传所有 query params + 审计。

    分页/时间窗/方向过滤参数（custAccountNo/startDate/endDate/pageNum/pageSize 等）
    由调用方带，gateway 不剪裁（契约 C）；对账/账单展示的证据面查询统一走本端点，
    上游不再直调 wedap。
    """
    params = dict(request.query_params)
    wedap = request.app.state.wedap
    data = await _audited_passthrough(
        request,
        endpoint="deposit/transactions",
        params=params,
        hdr=hdr,
        call=lambda: wedap.get_deposit_transactions(
            tenant_id=hdr["tenant_id"],
            request_id=hdr["request_id"],
            params=params,
        ),
    )
    return ok(data, trace_id=hdr["trace_id"])


@router.get("/api/v1/deposit/internal-accounts/info")
async def internal_account_info(
    request: Request,
    hdr: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    """内部户信息查询（对接文档 v0.4.0 §5.7 · 闭 D-1）：透传 + 审计 + 余额快照。

    内部户（归集/分发/退款中转方，如 INT00101001USD）余额是资金台账 Pool 侧的
    独立对账锚点——此前只能靠推算守恒。响应为单账户扁平形态（accountNo/
    accountBalance），与 balances/total 的 accounts[] 不同，故快照单独落一行；
    accountNo 缺失 / 余额不可解析时跳过快照并 warning，不让查询整体失败。
    """
    params = dict(request.query_params)
    wedap = request.app.state.wedap
    data = await _audited_passthrough(
        request,
        endpoint="deposit/internal-accounts/info",
        params=params,
        hdr=hdr,
        call=lambda: wedap.get_internal_account_info(
            tenant_id=hdr["tenant_id"],
            request_id=hdr["request_id"],
            params=params,
        ),
    )
    account_no = data.get("accountNo")
    raw_balance = data.get("accountBalance")
    if account_no is None or raw_balance is None:
        # 缺 accountNo 或缺余额都跳过——余额缺失不允许默认 0 落快照，
        # 0 会被当成真实 Pool 对账锚点造成假对平（codex HIGH）。
        logger.warning(
            "Skipping snapshot for internal account: missing accountNo/accountBalance in %r",
            data,
        )
        return ok(data, trace_id=hdr["trace_id"])
    try:
        balance = Decimal(str(raw_balance))
    except InvalidOperation:
        balance = None
    # NaN/Infinity 能通过 Decimal 构造但不是合法余额；adjusted()>=17 超出
    # Numeric(21,4) 的 17 位整数容量，落库会炸——一并跳过（codex HIGH）。
    if balance is None or not balance.is_finite() or balance.adjusted() >= 17:
        logger.warning(
            "Skipping snapshot for internal account %s: invalid accountBalance %r",
            account_no,
            raw_balance,
        )
        return ok(data, trace_id=hdr["trace_id"])
    try:
        async with request.app.state.session_factory() as session:
            async with session.begin():
                session.add(
                    BalanceSnapshot(
                        tenant_id=hdr["tenant_id"],
                        account_id=str(account_no),
                        balance=balance,
                        currency=str(data.get("currencyCode", "USD")),
                        source_endpoint="deposit/internal-accounts/info",
                        captured_at=dt.datetime.now(dt.UTC),
                    )
                )
    except SQLAlchemyError:
        # 快照是旁路增强：上游查询与审计已成功后，DB 抖动/落库失败不允许
        # 把 200 变 500（codex HIGH）。只告警，响应照常返回查询数据。
        logger.warning(
            "Snapshot write failed for internal account %s; query response unaffected",
            account_no,
            exc_info=True,
        )
    return ok(data, trace_id=hdr["trace_id"])
