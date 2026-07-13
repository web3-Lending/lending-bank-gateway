import asyncio
import json
import logging
import sys
import traceback
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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
from app.api.v1.deposit import router as deposit_router
from app.api.v1.health import router as health_router
from app.api.v1.loans import router as loans_router
from app.api.v1.recon_notify import router as recon_notify_router
from app.api.v1.wedap_import_enqueue import router as wedap_import_enqueue_router
from app.clients.s3 import S3FileClient
from app.clients.wedap import WedapClient
from app.core.config import Settings, get_settings
from app.core.context import IdentifierMiddleware, current_ids
from app.core.db import build_engine, build_session_factory
from app.core.envelope import err
from app.core.s2s import S2SMiddleware
from app.workers.supervisor import supervised

logger = logging.getLogger(__name__)

_logging_configured = False


def _log_time(created: float) -> str:
    """Aware UTC ISO-8601 with millisecond precision, e.g. 2026-07-13T06:44:02.284+00:00.
    lending time SSOT (11-datetime-timezone-standard §2/§6) mandates UTC internally; any
    local-offset (+0800) / camelCase an external platform needs is produced at the log
    agent / sink adapter, NOT in the app (05-observability-audit §2)."""
    dt = datetime.fromtimestamp(created, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}+00:00"


class _JsonLogFormatter(logging.Formatter):
    """Single-line structured JSON to stdout so 华为云 ICAgent / CloudWatch Agent
    ingest without an app-side parse rule — no code change when either is added.
    Internal format is UTC + snake_case; agent出口 转外部平台格式。"""

    def format(self, record: logging.LogRecord) -> str:
        try:
            from app.core.context import current_ids

            trace_id = current_ids().trace_id
        except Exception:
            trace_id = "trc-none"
        entry: dict = {
            "time": _log_time(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": trace_id,
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = traceback.format_exception(*record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def _configure_logging(level: str) -> None:
    """给 root logger 装 stdout handler，使 app.* 的 INFO/exception 在容器 docker logs 可见。

    背景：此前 app 只 `logging.getLogger(__name__)` 而进程从未配 root handler，
    app.main 的 `worker tasks started`、supervised 崩溃 `logger.exception`、
    order-reconcile 的 warning 全部被 root 默认 WARNING lastResort 吞掉——运维在
    `docker logs` 里看不到 3 个 worker 是否在跑 / 是否崩溃 / 是否扫到候选。

    uvicorn 自带 logger（propagate=False）有独立 handler，不受本配置影响；这里只补
    root 这条传播链。幂等：模块级 `_logging_configured` 防止多次 create_app（测试）
    重复装 handler 导致日志行重复；level 非法时回退 INFO。
    """
    global _logging_configured
    root = logging.getLogger()
    resolved = logging.getLevelName(level.upper())
    if not isinstance(resolved, int):  # getLevelName 对非法名返回 "Level XXX" 字符串
        resolved = logging.INFO
    root.setLevel(resolved)
    if not _logging_configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonLogFormatter())
        root.addHandler(handler)
        _logging_configured = True


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
    """leg 同步/父单聚合 + （终态时）outbox 转发 —— **同一事务原子提交**（A-C-002）。

    外呼 get_composite_steps 在事务外预取（避免长事务持锁）；随后单事务内 apply_legs
    一起提交。V2 同步优先：**转发已并入 apply_legs 的终态收口**（finalize_terminal_in_session，
    仅 SUCCEEDED/FAILED 转发，dedup_key 用业务稳定键 fwd-{tenant}-{biz}-{status}）——与同步/
    兜底路径共用同一收口，消除「同步终态/回调终态各转一次」的双事件分叉。非终态（pending leg
    → PROCESSING）不转发，lifecycle 等终态。崩溃则整体回滚，inbox 留 RECEIVED 由重放幂等再驱动。
    """
    from app.services.legs import apply_legs_in_session

    biz_seq_no = str(body.get("bizSeqNo", ""))
    # 外呼预取 steps（事务外）
    steps = await request.app.state.wedap.get_composite_steps(
        tenant_id=tenant_id, biz_seq_no=biz_seq_no
    )
    # 单事务：leg 同步/父单聚合（终态时内部 finalize 转发）原子提交
    async with request.app.state.session_factory() as session:
        async with session.begin():
            await apply_legs_in_session(
                session,
                tenant_id=tenant_id,
                biz_seq_no=biz_seq_no,
                steps=steps,
                source="CALLBACK",
                trace_id=trace_id,
            )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan：startup 时按 workers_enabled 起后台 asyncio task；shutdown 时 cancel。"""
    settings: Settings = app.state.settings
    tasks: list[asyncio.Task[None]] = []
    worker_engine: AsyncEngine | None = None

    if settings.workers_enabled:
        from app.workers import (
            order_reconcile_worker,
            outbox_dispatcher,
            recon_worker,
        )

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
        tasks.append(
            asyncio.create_task(
                supervised(
                    "order-reconcile-worker",
                    lambda: order_reconcile_worker.run_forever(
                        worker_factory,
                        wedap=app.state.wedap,
                        interval_seconds=settings.order_reconcile_interval_seconds,
                        stale_after_seconds=settings.order_reconcile_stale_after_seconds,
                        max_age_seconds=settings.order_reconcile_max_age_seconds,
                        leg_backfill_seconds=settings.order_reconcile_leg_backfill_seconds,
                        batch_limit=settings.order_reconcile_batch_limit,
                    ),
                    restart_delay_seconds=settings.worker_restart_delay_seconds,
                ),
                name="order-reconcile-worker",
            )
        )
        if settings.wedap_delivery_enabled:
            from functools import partial

            from app.clients.recon_callback import ReconCallbackClient
            from app.services.wedap_delivery import compute_result_deadline
            from app.workers import wedap_delivery_dispatcher

            wedap_deliver = wedap_delivery_dispatcher.make_deliver(
                S3FileClient(endpoint_url=settings.s3_endpoint_url),
                app.state.wedap,
                staging_bucket=settings.wedap_staging_bucket,
                wedap_bucket=settings.wedap_import_bucket,
                presigned_enabled=settings.wedap_presigned_enabled,
            )
            wedap_on_terminal = wedap_delivery_dispatcher.make_on_terminal(
                ReconCallbackClient(
                    base_url=settings.recon_base_url,
                    hmac_secret=settings.recon_callback_hmac_secret or None,
                ),
                worker_factory,
            )
            # result 回收（Phase1）：拉 wedap 写回的 _result.json → 转投 recon line-results
            wedap_result_fetch, wedap_result_post = wedap_delivery_dispatcher.make_collect(
                S3FileClient(endpoint_url=settings.s3_endpoint_url),
                ReconCallbackClient(
                    base_url=settings.recon_base_url,
                    hmac_secret=settings.recon_callback_hmac_secret or None,
                ),
                wedap_bucket=settings.wedap_import_bucket,
                wedap_client=app.state.wedap,
                presigned_enabled=settings.wedap_presigned_enabled,
            )
            # §6.1 护栏②：受理时落 result 回收截止（下一个 wedap scanner 窗口 + grace）
            wedap_result_deadline = partial(
                compute_result_deadline,
                anchor_hour=settings.wedap_result_scan_anchor_hour,
                grace_minutes=settings.wedap_result_grace_minutes,
            )
            tasks.append(
                asyncio.create_task(
                    supervised(
                        "wedap-delivery-dispatcher",
                        lambda: wedap_delivery_dispatcher.run_forever(
                            worker_factory,
                            deliver=wedap_deliver,
                            max_attempts=settings.wedap_delivery_max_attempts,
                            interval_seconds=settings.wedap_delivery_interval_seconds,
                            on_terminal=wedap_on_terminal,
                            result_fetch=wedap_result_fetch,
                            result_post=wedap_result_post,
                            result_deadline=wedap_result_deadline,
                            # §6.1 护栏③④：PENDING_STUCK / RESULT_OVERDUE 去重告警
                            pending_max_age_seconds=(
                                settings.wedap_delivery_pending_max_age_seconds
                            ),
                            alert_batch_limit=settings.wedap_delivery_alert_batch_limit,
                        ),
                        restart_delay_seconds=settings.worker_restart_delay_seconds,
                    ),
                    name="wedap-delivery-dispatcher",
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
    _configure_logging(settings.log_level)
    if settings.env not in ("local", "test") and not settings.s2s_secret:
        raise RuntimeError(
            "GW_S2S_SECRET 必须在非 local/test 环境配置（fail-fast，资金网关禁 fail-open）"
        )
    if settings.env not in ("local", "test") and not settings.wedap_callback_api_key:
        raise RuntimeError(
            "GW_WEDAP_CALLBACK_API_KEY 必须在非 local/test 环境配置"
            "（wedap 回调入口 fail-fast，资金网关禁 fail-open）"
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
        exempt_paths={"/healthz", "/readyz", "/build-info"},
        allowed_callers=allowed_callers,
        caller_tokens=caller_tokens,
        # wedap→gateway 入站回调（外部 wedap 无 lending S2S token）：middleware 层 apikey 认证，
        # body 解析前拒绝、最小权限（仅这两个 path，wedap 打不了银行 API）。
        callback_paths={
            "/api/v1/callbacks/wedap/transactions",
            "/api/v1/recon/notify",
        },
        callback_api_key=settings.wedap_callback_api_key,
    )
    app.add_middleware(IdentifierMiddleware)
    engine = build_engine(
        settings.db_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    # 资金链路半配置 fail-fast（codex MEDIUM）：银行 APISIX 模式(签名密钥配置)必须同时配 apikey，
    # 否则会发 apikey:"" 到 APISIX、首笔交易才被 key-auth 拒；启动期拦截而非运行期爆。
    if settings.wedap_bank_signing_secret and not settings.wedap_bank_api_key:
        raise RuntimeError(
            "GW_WEDAP_BANK_SIGNING_SECRET is set but GW_WEDAP_BANK_API_KEY is empty — "
            "bank APISIX mode requires both (or clear both for direct-baffle mode)."
        )
    app.state.wedap = WedapClient(
        base_url=settings.wedap_base_url,
        timeout_seconds=settings.wedap_timeout_seconds,
        import_api_key=settings.wedap_import_api_key,
        import_signing_secret=settings.wedap_import_signing_secret,
        bank_api_key=settings.wedap_bank_api_key,
        bank_signing_secret=settings.wedap_bank_signing_secret,
    )
    # outbox_targets 供 dispatcher worker（T24）读取；key=target 名，value=目标 URL
    app.state.outbox_targets = {
        "lifecycle": settings.callback_target_lifecycle_url,
    }
    app.state.callback_after_ingest = _after_ingest
    app.include_router(health_router)
    app.include_router(loans_router)
    app.include_router(bank_funds_router)
    app.include_router(callbacks.router)
    app.include_router(admin_ops_router)
    app.include_router(recon_notify_router)
    app.include_router(deposit_router)
    app.include_router(wedap_import_enqueue_router)
    return app


app = create_app()
