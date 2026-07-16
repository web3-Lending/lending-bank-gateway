"""admin 端点：平台账户白名单登记维护（account guard 配置面）。

边界声明：这是「白名单登记」，**非账户主数据 CRUD**——不存余额 / 开户信息 /
户主，只登记「哪些平台账户允许走 gateway + 允许哪些 business_scope」。
数据来源是资金运营；初始批量 seed 走 alembic data migration，日常增改走本端点。

鉴权：全局 S2SMiddleware（谁能调 gateway）之上叠加 GW_ADMIN_CALLERS 白名单
（谁能改 enforcement 配置）。GW_ADMIN_CALLERS 为空 = fail-closed 全拒——
守门人配置面比业务接口更敏感，未显式授权就不该有人能读写白名单。
所有写操作落 audit_log（hash 链）。
"""

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.context import current_ids
from app.core.envelope import ok
from app.models.platform_account import PlatformBankAccount
from app.services.audit import write_audit

router = APIRouter(prefix="/api/v1/admin/platform-accounts", tags=["admin"])

_SCOPE_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")
_STATUSES = {"active", "disabled"}


def _require_admin_caller(request: Request) -> str:
    caller = request.headers.get("X-Caller-Service", "")
    settings = request.app.state.settings
    allowed = {c.strip() for c in settings.admin_callers.split(",") if c.strip()}
    if not allowed or caller not in allowed:
        raise HTTPException(
            403,
            detail={
                "code": "GW_403_ADMIN_CALLER",
                "message": "caller not allowed for platform-account admin",
            },
        )
    # 共享 secret 模式下 X-Caller-Service 可伪造（codex R2 P0）：非 local/test 环境
    # 要求本请求经 per-service token 认证（S2SMiddleware 打的 s2s_token_bound 标记；
    # create_app fail-fast 已保证 admin caller 必有专属 token，合法请求必然带标）。
    if settings.env not in ("local", "test") and not getattr(
        request.state, "s2s_token_bound", False
    ):
        raise HTTPException(
            403,
            detail={
                "code": "GW_403_ADMIN_CALLER",
                "message": "admin caller must authenticate with per-service token",
            },
        )
    return caller


def _validate_scopes_csv(raw: str) -> str:
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens or any(not _SCOPE_TOKEN.match(t) for t in tokens):
        raise HTTPException(
            400,
            detail={
                "code": "GW_400_VALIDATION",
                "message": f"invalid allowedScopes csv: {raw!r}",
            },
        )
    return ",".join(tokens)


def _row_out(r: PlatformBankAccount) -> dict[str, object]:
    return {
        "id": r.id,
        "tenantId": r.tenant_id,
        "accountNo": r.account_no,
        "purpose": r.purpose,
        "allowedScopes": r.allowed_scopes,
        "currency": r.currency,
        "status": r.status,
        "note": r.note,
        "updatedAt": r.updated_at.isoformat() if r.updated_at else None,
    }


class PlatformAccountCreate(BaseModel):
    tenantId: str = Field(min_length=1, max_length=36)
    accountNo: str = Field(min_length=1, max_length=64)
    purpose: str = Field(min_length=1, max_length=24)
    allowedScopes: str = Field(min_length=1, max_length=255)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    status: str = "active"
    note: str | None = Field(default=None, max_length=255)


class PlatformAccountPatch(BaseModel):
    purpose: str | None = Field(default=None, min_length=1, max_length=24)
    allowedScopes: str | None = Field(default=None, min_length=1, max_length=255)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    status: str | None = None
    note: str | None = Field(default=None, max_length=255)


@router.get("")
async def list_platform_accounts(
    request: Request, tenantId: str = Query(min_length=1)
) -> dict[str, object]:
    _require_admin_caller(request)
    trace_id = current_ids().trace_id
    factory = request.app.state.session_factory
    stmt = (
        select(PlatformBankAccount)
        .where(PlatformBankAccount.tenant_id == tenantId)
        .order_by(PlatformBankAccount.id)
    )
    async with factory() as session:
        result = await session.execute(stmt)
        rows = result.scalars().all()
    return ok({"items": [_row_out(r) for r in rows], "count": len(rows)}, trace_id=trace_id)


@router.post("")
async def create_platform_account(
    body: PlatformAccountCreate, request: Request
) -> dict[str, object]:
    caller = _require_admin_caller(request)
    trace_id = current_ids().trace_id
    if body.status not in _STATUSES:
        raise HTTPException(
            400,
            detail={"code": "GW_400_VALIDATION", "message": f"invalid status {body.status!r}"},
        )
    scopes = _validate_scopes_csv(body.allowedScopes)
    account_no = body.accountNo.strip()
    factory = request.app.state.session_factory
    try:
        async with factory() as session:
            async with session.begin():
                row = PlatformBankAccount(
                    tenant_id=body.tenantId,
                    account_no=account_no,
                    purpose=body.purpose,
                    allowed_scopes=scopes,
                    currency=body.currency,
                    status=body.status,
                    note=body.note,
                )
                session.add(row)
                await session.flush()
                # server_default 列（created_at/updated_at）flush 后未加载，
                # 显式 refresh 再快照——出事务块后属性过期，同步访问会 MissingGreenlet。
                await session.refresh(row)
                out = _row_out(row)
                audit_kwargs: dict[str, Any] = {
                    "tenant_id": body.tenantId,
                    "actor": f"svc:{caller}",
                    "action": "platform_account.create",
                    "entity": "platform_account",
                    "payload": out,
                }
                await write_audit(session, **audit_kwargs)
    except IntegrityError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "GW_409_DUPLICATE",
                "message": f"platform account already registered: {account_no}",
            },
        ) from exc
    return ok(out, trace_id=trace_id)


@router.patch("/{row_id}")
async def patch_platform_account(
    row_id: int, body: PlatformAccountPatch, request: Request
) -> dict[str, object]:
    caller = _require_admin_caller(request)
    trace_id = current_ids().trace_id
    # model_fields_set 区分「字段缺省=不动」与「显式 null=清空」（codex P2）：
    # currency/note 可显式 null 清回不限币种/无备注；purpose/allowedScopes/status
    # 是必填语义列，显式 null 一律 400。
    sent = body.model_fields_set
    for required_field in ("purpose", "allowedScopes", "status"):
        if required_field in sent and getattr(body, required_field) is None:
            raise HTTPException(
                400,
                detail={
                    "code": "GW_400_VALIDATION",
                    "message": f"{required_field} cannot be null",
                },
            )
    if body.status is not None and body.status not in _STATUSES:
        raise HTTPException(
            400,
            detail={"code": "GW_400_VALIDATION", "message": f"invalid status {body.status!r}"},
        )
    scopes = _validate_scopes_csv(body.allowedScopes) if body.allowedScopes is not None else None
    factory = request.app.state.session_factory
    async with factory() as session:
        async with session.begin():
            stmt = select(PlatformBankAccount).where(PlatformBankAccount.id == row_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                raise HTTPException(
                    404,
                    detail={
                        "code": "GW_404_PLATFORM_ACCOUNT",
                        "message": f"platform account {row_id} not found",
                    },
                )
            changed: dict[str, object] = {}
            if body.purpose is not None:
                row.purpose = body.purpose
                changed["purpose"] = body.purpose
            if scopes is not None:
                row.allowed_scopes = scopes
                changed["allowedScopes"] = scopes
            if "currency" in sent:
                row.currency = body.currency
                changed["currency"] = body.currency
            if body.status is not None:
                row.status = body.status
                changed["status"] = body.status
            if "note" in sent:
                row.note = body.note
                changed["note"] = body.note
            if changed:
                audit_kwargs: dict[str, Any] = {
                    "tenant_id": row.tenant_id,
                    "actor": f"svc:{caller}",
                    "action": "platform_account.update",
                    "entity": "platform_account",
                    "payload": {"id": row_id, "accountNo": row.account_no, "changed": changed},
                }
                await write_audit(session, **audit_kwargs)
            # onupdate=func.now() 在 flush 后使 updated_at 过期，同步快照会
            # MissingGreenlet——显式 flush+refresh 后再取。
            await session.flush()
            await session.refresh(row)
            out = _row_out(row)
    return ok(out, trace_id=trace_id)
