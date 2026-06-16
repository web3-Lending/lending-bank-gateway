import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import callbacks
from app.api.v1.admin_ops import router as admin_ops_router
from app.api.v1.bank_funds import router as bank_funds_router
from app.api.v1.composite import router as composite_router
from app.api.v1.deposit import router as deposit_router
from app.api.v1.fiat_vault import router as fiat_vault_router
from app.api.v1.health import router as health_router
from app.api.v1.loans import router as loans_router
from app.api.v1.recon_notify import router as recon_notify_router
from app.clients.s3 import S3FileClient
from app.clients.wedap import WedapClient
from app.core.config import Settings, get_settings
from app.core.context import IdentifierMiddleware, current_ids
from app.core.db import build_engine, build_session_factory
from app.core.envelope import err
from app.core.s2s import S2SMiddleware
from app.services.outbox import enqueue_forward
from app.workers.supervisor import supervised

logger = logging.getLogger(__name__)


def _resolve_trace_id(request: Request) -> str:
    """解析当前请求的 trace_id。

    优先从 contextvar 获取；若 IdentifierMiddleware 尚未运行（如 ServerErrorMiddleware
    在外层捕获异常时 contextvar 已 reset），则回退到请求头 X-Trace-Id；
    仍无则返回 "trc-none"。
    """
    trace_id = current_ids().trace_id
    if trace_id == "trc-none":
        trace_id = request.headers.get("X-Trace-Id") or "trc-none"
    return trace_id


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """统一处理 FastAPI HTTPException 与 Starlette 路由 miss 404。

    两者均继承 StarletteHTTPException，注册同一个 handler。
    """
    trace_id = _resolve_trace_id(request)
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
    trace_id = _resolve_trace_id(request)
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
    trace_id = _resolve_trace_id(request)
    logger.exception("unhandled exception trace_id=%s", trace_id)
    return JSONResponse(
        err("GW_500_INTERNAL", "internal error", trace_id=trace_id),
        status_code=500,
    )


async def _after_ingest(
    request: Request, *, tenant_id: str, body: dict[str, Any], request_id: str, trace_id: str
) -> None:
    """leg 同步/父单聚合 + outbox 转发入队 —— **同一事务原子提交**（A-C-002）。

    外呼 get_composite_steps 在事务外预取（避免长事务持锁）；随后单事务内 apply_legs +
    enqueue_forward 一起提交。这样消除「leg 已落库但 outbox 未入队」的崩溃窗口——
    崩溃则整体回滚，inbox 留 RECEIVED 由重放幂等再驱动（apply 幂等 upsert + enqueue
    fwd-{request_id} 去重）。request_id 作 outbox dedup_key 保证跨重放只产生一条转发。
    """
    from app.services.legs import apply_legs_in_session

    biz_seq_no = str(body.get("bizSeqNo", ""))
    # 外呼预取 steps（事务外）
    steps = await request.app.state.wedap.get_composite_steps(
        tenant_id=tenant_id, biz_seq_no=biz_seq_no
    )
    # 单事务：leg 同步/父单聚合 + outbox enqueue 原子提交
    async with request.app.state.session_factory() as session:
        async with session.begin():
            await apply_legs_in_session(
                session, tenant_id=tenant_id, biz_seq_no=biz_seq_no, steps=steps
            )
            await enqueue_forward(
                session,
                tenant_id=tenant_id,
                target="lifecycle",
                payload=body,
                dedup_key=f"fwd-{request_id}",
                trace_id=trace_id,
            )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan：startup 时按 workers_enabled 起后台 asyncio task；shutdown 时 cancel。"""
    settings: Settings = app.state.settings
    tasks: list[asyncio.Task[None]] = []
    worker_engine: AsyncEngine | None = None

    if settings.workers_enabled:
        from app.workers import outbox_dispatcher, recon_worker

        # 独立连接池（A-M-003）：worker 慢外呼/长事务不与 API 在线请求争抢同一连接池
        worker_engine = build_engine(
            settings.db_url,
            pool_size=settings.worker_db_pool_size,
            max_overflow=settings.worker_db_max_overflow,
        )
        worker_factory = build_session_factory(worker_engine)

        tasks.append(
            asyncio.create_task(
                supervised(
                    "outbox-dispatcher",
                    lambda: outbox_dispatcher.run_forever(
                        worker_factory,
                        targets=app.state.outbox_targets,
                        max_attempts=settings.outbox_max_attempts,
                        interval_seconds=settings.outbox_interval_seconds,
                    ),
                    restart_delay_seconds=settings.worker_restart_delay_seconds,
                ),
                name="outbox-dispatcher",
            )
        )
        tasks.append(
            asyncio.create_task(
                supervised(
                    "recon-worker",
                    lambda: recon_worker.run_forever(
                        worker_factory,
                        s3=S3FileClient(endpoint_url=settings.s3_endpoint_url),
                        archive_dir=settings.archive_dir,
                        interval_seconds=settings.recon_interval_seconds,
                    ),
                    restart_delay_seconds=settings.worker_restart_delay_seconds,
                ),
                name="recon-worker",
            )
        )
        logger.info(
            "worker tasks started (dedicated pool): outbox_interval=%.1fs recon_interval=%.1fs",
            settings.outbox_interval_seconds,
            settings.recon_interval_seconds,
        )

    yield

    if tasks:  # pragma: no cover
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("worker tasks cancelled on shutdown")
    if worker_engine is not None:  # pragma: no cover
        await worker_engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    # 配置注入：默认走 get_settings() 工厂；测试/多环境可显式传入隔离实例，
    # 不再依赖 lru_cache 全局单例 + cache_clear（A-M-002）。
    if settings is None:
        settings = get_settings()
    if settings.env not in ("local", "test") and not settings.s2s_secret:
        raise RuntimeError(
            "GW_S2S_SECRET 必须在非 local/test 环境配置（fail-fast，资金网关禁 fail-open）"
        )

    app = FastAPI(title="lending-bank-gateway", version="0.1.0", lifespan=_lifespan)
    # 全进程统一从 app.state.settings 取配置（lifespan/worker 不再各自调 get_settings）
    app.state.settings = settings

    # 解析 caller 白名单：空串 = 不启用
    allowed_callers: set[str] | None = (
        {c.strip() for c in settings.s2s_callers.split(",") if c.strip()}
        if settings.s2s_callers.strip()
        else None
    )
    # 解析 per-service token（A-m-002）：`caller:token,...`；空串 = 不启用，回退共享 secret
    caller_tokens: dict[str, str] | None = None
    if settings.s2s_caller_tokens.strip():
        caller_tokens = {}
        for pair in settings.s2s_caller_tokens.split(","):
            if ":" in pair:
                name, _, tok = pair.partition(":")
                name, tok = name.strip(), tok.strip()
                if name and tok:
                    caller_tokens[name] = tok
        caller_tokens = caller_tokens or None

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
        caller_tokens=caller_tokens,
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
    app.include_router(fiat_vault_router)
    app.include_router(deposit_router)
    return app


app = create_app()
