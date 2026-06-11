"""app/api/v1/deposit.py

deposit/users 查询透传端点：
- GET /api/v1/deposit/balances/total  → 余额汇总（审计 + 快照）
- GET /api/v1/deposit/accounts        → 账户列表（仅审计）
- GET /api/v1/users/info              → 用户信息（仅审计）

所有端点均写 QueryAudit；balances/total 额外写 BalanceSnapshot。
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
from fastapi import APIRouter, HTTPException, Request

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
async def deposit_balance_total(request: Request, userId: str) -> dict[str, Any]:
    """余额汇总查询：透传 + 审计 + 余额快照（BalanceSnapshot）。"""
    hdr = require_headers(request)
    wedap = request.app.state.wedap
    data = await _audited_passthrough(
        request,
        endpoint="deposit/balances/total",
        params={"userId": userId},
        hdr=hdr,
        call=lambda: wedap.get_deposit_balance_total(
            tenant_id=hdr["tenant_id"],
            request_id=hdr["request_id"],
            user_id=userId,
        ),
    )
    now = dt.datetime.now(dt.UTC)
    async with request.app.state.session_factory() as session:
        async with session.begin():
            for acct in data.get("accounts", []):
                try:
                    balance = Decimal(str(acct.get("balance", "0")))
                except InvalidOperation:
                    logger.warning(
                        "Skipping snapshot for account %s: invalid balance %r",
                        acct.get("custAccountNo"),
                        acct.get("balance"),
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
    return ok(data, trace_id=hdr["trace_id"])


@router.get("/api/v1/deposit/accounts")
async def deposit_accounts(request: Request, userId: str) -> dict[str, Any]:
    """账户列表查询：透传 + 审计（无快照）。"""
    hdr = require_headers(request)
    wedap = request.app.state.wedap
    data = await _audited_passthrough(
        request,
        endpoint="deposit/accounts",
        params={"userId": userId},
        hdr=hdr,
        call=lambda: wedap.get_deposit_accounts(
            tenant_id=hdr["tenant_id"],
            request_id=hdr["request_id"],
            user_id=userId,
        ),
    )
    return ok(data, trace_id=hdr["trace_id"])


@router.get("/api/v1/users/info")
async def users_info(request: Request) -> dict[str, Any]:
    """用户信息查询：原样透传所有 query params + 审计。"""
    hdr = require_headers(request)
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
