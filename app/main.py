import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import callbacks
from app.api.v1.admin_ops import router as admin_ops_router
from app.api.v1.bank_funds import router as bank_funds_router
from app.api.v1.composite import router as composite_router
from app.api.v1.health import router as health_router
from app.api.v1.loans import router as loans_router
from app.api.v1.recon_notify import router as recon_notify_router
from app.clients.wedap import WedapClient
from app.core.config import get_settings
from app.core.context import IdentifierMiddleware, current_ids
from app.core.db import build_engine, build_session_factory
from app.core.envelope import err
from app.core.s2s import S2SMiddleware
from app.services.outbox import enqueue_forward

logger = logging.getLogger(__name__)


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """统一处理 FastAPI HTTPException 与 Starlette 路由 miss 404。

    两者均继承 StarletteHTTPException，注册同一个 handler。
    """
    trace_id = current_ids().trace_id
    detail = exc.detail
    if isinstance(detail, dict):
        code = detail.get("code", f"GW_{exc.status_code}")
        message = detail.get("message", str(detail))
    else:
        code = f"GW_{exc.status_code}"
        message = str(detail)
    return JSONResponse(
        err(code, message, trace_id=trace_id),
        status_code=exc.status_code,
    )


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    trace_id = current_ids().trace_id
    # exc.errors() 的 ctx 字段可能含不可序列化对象，用 jsonable_encoder 净化
    errors = jsonable_encoder(exc.errors())
    return JSONResponse(
        err(
            "GW_422_VALIDATION",
            "request validation failed",
            trace_id=trace_id,
            details={"errors": errors},
        ),
        status_code=422,
    )


async def _generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = current_ids().trace_id
    logger.exception("unhandled exception trace_id=%s", trace_id)
    return JSONResponse(
        err("GW_500_INTERNAL", "internal error", trace_id=trace_id),
        status_code=500,
    )


async def _after_ingest(
    request: Request, *, tenant_id: str, body: dict[str, Any], request_id: str
) -> None:
    """T16 接线：leg 同步 + 父单聚合。T17 outbox 转发：独立事务 enqueue。

    request_id 作为 outbox dedup_key（fwd-{request_id}），确保同一 inbox 请求
    即使重放也只产生一条 outbox 行（下游幂等键跨重放稳定）。
    """
    from app.services.legs import sync_legs_for

    await sync_legs_for(
        request.app.state.session_factory,
        wedap=request.app.state.wedap,
        tenant_id=tenant_id,
        biz_seq_no=str(body.get("bizSeqNo", "")),
    )

    # T17：outbox enqueue（独立事务，与 legs 隔离）
    async with request.app.state.session_factory() as session:
        async with session.begin():
            await enqueue_forward(
                session,
                tenant_id=tenant_id,
                target="lifecycle",
                payload=body,
                dedup_key=f"fwd-{request_id}",
            )


def create_app() -> FastAPI:
    app = FastAPI(title="lending-bank-gateway", version="0.1.0")
    settings = get_settings()
    if settings.env not in ("local", "test") and not settings.s2s_secret:
        raise RuntimeError(
            "GW_S2S_SECRET 必须在非 local/test 环境配置（fail-fast，资金网关禁 fail-open）"
        )

    # 解析 caller 白名单：空串 = 不启用
    allowed_callers: set[str] | None = (
        {c.strip() for c in settings.s2s_callers.split(",") if c.strip()}
        if settings.s2s_callers.strip()
        else None
    )

    # 同时注册 Starlette 基类（路由 miss 404）和 FastAPI 子类（显式 raise HTTPException）
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _generic_exception_handler)

    # starlette add_middleware 是栈式：后 add 先执行
    # 执行顺序：IdentifierMiddleware → S2SMiddleware → handler
    app.add_middleware(
        S2SMiddleware,
        secret=settings.s2s_secret,
        exempt_paths={"/healthz", "/readyz"},
        allowed_callers=allowed_callers,
    )
    app.add_middleware(IdentifierMiddleware)
    engine = build_engine(
        settings.db_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    app.state.wedap = WedapClient(
        base_url=settings.wedap_base_url,
        timeout_seconds=settings.wedap_timeout_seconds,
    )
    # outbox_targets 供 dispatcher worker（T24）读取；key=target 名，value=目标 URL
    app.state.outbox_targets = {
        "lifecycle": settings.callback_target_lifecycle_url,
    }
    app.state.callback_after_ingest = _after_ingest
    app.include_router(health_router)
    app.include_router(loans_router)
    app.include_router(bank_funds_router)
    app.include_router(composite_router)
    app.include_router(callbacks.router)
    app.include_router(admin_ops_router)
    app.include_router(recon_notify_router)
    return app


app = create_app()
