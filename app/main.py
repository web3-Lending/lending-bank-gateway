import asyncio
import json
import logging
import re
import sys
import traceback
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import money_write_error_extra
from app.api.v1 import callbacks
from app.api.v1.admin_ops import router as admin_ops_router
from app.api.v1.bank_funds import router as bank_funds_router
from app.api.v1.deposit import router as deposit_router
from app.api.v1.health import router as health_router
from app.api.v1.loans import router as loans_router
from app.api.v1.platform_accounts import router as platform_accounts_router
from app.api.v1.recon_notify import router as recon_notify_router
from app.api.v1.wedap_import_enqueue import router as wedap_import_enqueue_router
from app.clients.s3 import S3FileClient
from app.clients.wedap import WedapClient
from app.core.config import Settings, get_settings
from app.core.context import (
    IdentifierMiddleware,
    RequestTargetLimitMiddleware,
    apply_no_store,
    current_ids,
    sanitize_correlation_id,
)
from app.core.db import build_engine, build_session_factory
from app.core.envelope import err
from app.core.s2s import S2SMiddleware, parse_caller_tokens
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

            ids = current_ids()
            trace_id = ids.trace_id
            # 回显给调用方的那个受控 X-Request-Id 必须同时落日志，否则调用方拿着
            # 响应头来报障时服务端零命中，§11.1 的「响应↔日志回链」只完成 trace 一半。
            request_id = ids.safe_request_id
        except Exception:
            trace_id = "trc-none"
            request_id = "req-none"
        entry: dict[str, object] = {
            "time": _log_time(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": trace_id,
            "request_id": request_id,
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = traceback.format_exception(*record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


# §8.5：日志同样禁止记录完整 PII 与未声明 query 原值。httpx 的 INFO 行形如
# `HTTP Request: GET http://host/path?userId=U1 "HTTP/1.1 200 OK"`，会把上游 URL 的
# query 原值（deposit/users 系列含 userId 等 PII）写进 stdout。
_URL_QUERY_RE = re.compile(r'(?P<url>https?://[^\s"?]*)\?[^\s"]*')


class _QueryRedactingFilter(logging.Filter):
    """把日志文本里任意 http(s) URL 的 query 段整体替换为 `?<redacted>`。

    保留 method / host / path / 上游状态这些排障必需信息，只抹掉参数值——比整体
    把 httpx logger 降级到 WARNING 更可用：上游调用仍然可见，PII 不再落盘。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _URL_QUERY_RE.sub(r"\g<url>?<redacted>", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


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
        handler.addFilter(_QueryRedactingFilter())
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
        # 回退读的是原始 header——与 IdentifierMiddleware 走同一把校验/重签，
        # 否则畸形值会绕过中间件从这条回退路径进响应体。
        trace_id = sanitize_correlation_id(request.headers.get("X-Trace-Id"), prefix="trc") or (
            "trc-none"
        )
    return trace_id


def _resolve_safe_request_id(request: Request) -> str:
    """解析当前请求的受控回显 X-Request-Id（§7.4「所有响应」）。

    唯一调用方 `_generic_exception_handler` 挂在 ServerErrorMiddleware 上——它在所有
    用户中间件之外，异常冒泡出去时 IdentifierMiddleware 的 `finally` 已 reset 掉
    contextvar，所以这里**不读 contextvar**（读到的必然是默认值），直接回退读原始
    header 并走与中间件同一把校验/重签，仍无则新签——保证 500 也带得回关联 id。
    """
    return sanitize_correlation_id(request.headers.get("X-Request-Id"), prefix="req") or (
        f"req-{uuid.uuid4().hex}"
    )


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """统一处理 FastAPI HTTPException 与 Starlette 路由 miss 404。

    两者均继承 StarletteHTTPException，注册同一个 handler。
    """
    trace_id = _resolve_trace_id(request)
    detail = exc.detail
    extra: dict[str, object] = {}
    if isinstance(detail, dict):
        code = detail.get("code", f"GW_{exc.status_code}")
        message = detail.get("message", str(detail))
        # code/message 之外的键（如 account guard 的 reason 细分）透传进
        # error.details——否则调用方只见笼统 message，无法程序化分支。
        extra = {k: v for k, v in detail.items() if k not in {"code", "message"}}
    else:
        code = f"GW_{exc.status_code}"
        message = str(detail)
    # v2.2 §8.2：MONEY_WRITE 的写错误必须带 typed 字段（raise 点自带的同名键优先，
    # 见 app/api/deps.money_write_error_extra 的分界线说明）。
    extra = {**money_write_error_extra(request, exc.status_code), **extra}
    return JSONResponse(
        err(code, message, trace_id=trace_id, details=extra or None),
        status_code=exc.status_code,
        # API-HTTP-004/006：必须透传 exc.headers。Starlette 的 method-mismatch 405 自带
        # `Allow`，认证类 HTTPException 自带 `WWW-Authenticate`——本 handler 一旦丢弃
        # exc.headers，等于在全局把这些强制响应头删掉，且任何下游 handler 都补不回来。
        headers=exc.headers,
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
            # schema 校验失败必然发生在进服务之前 → MONEY_WRITE 端点上是确证的
            # 「未产生影响」（§8.2），普通端点这里恒为空 dict。
            details={"errors": errors, **money_write_error_extra(request, 422)},
        ),
        status_code=422,
    )


async def _generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = _resolve_trace_id(request)
    safe_request_id = _resolve_safe_request_id(request)
    # 两个 id 直接写进 message：本 handler 运行时 contextvar 已 reset，
    # _JsonLogFormatter 的 trace_id/request_id 字段只会是默认值，对不上响应头。
    logger.exception("unhandled exception trace_id=%s request_id=%s", trace_id, safe_request_id)
    response = JSONResponse(
        err("GW_500_INTERNAL", "internal error", trace_id=trace_id),
        status_code=500,
    )
    # 本 handler 挂在 ServerErrorMiddleware 上——它在所有用户中间件之外，响应不回经
    # IdentifierMiddleware，所以 no-store 与两个关联 id 头都必须在这里自己补
    # （API-HTTP-015 错误响应默认 no-store；§7.4「所有响应必须返回受控 X-Request-Id」
    # + §11.1 响应↔日志回链）。未处理异常恰恰是调用方一定会来报障的那一类响应，
    # 少了关联 id 就没法把这次响应对上服务端日志。
    apply_no_store(response)
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Request-Id"] = safe_request_id
    return response


async def _after_ingest(
    request: Request, *, tenant_id: str, body: dict[str, Any], request_id: str, trace_id: str
) -> None:
    """回调路径 order 级终态收口（C5：组合交易 leg/step 对 lending 透明，不下钻明细）。

    body txnStatus 即父单权威终态 → CAS + finalize_terminal_in_session（finalized_at/via +
    audit + 转发 lifecycle，dedup_key 用业务稳定键 fwd-{tenant}-{biz}-{status}）——与同步/
    兜底路径共用同一收口。body 无终态信息则回落 wedap status-query（order 级）主动收敛；
    仍未收敛抛 CallbackTerminalUnresolved，inbox 留 RECEIVED 由重放幂等再驱动。
    """
    from app.services.callback_finalize import resolve_callback_terminal

    await resolve_callback_terminal(
        request.app.state.session_factory,
        wedap=request.app.state.wedap,
        tenant_id=tenant_id,
        body=body,
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
            # §6.1 护栏②：受理时落 result 回收截止（受理 + wedap 看门狗窗口 24h + 缓冲）
            wedap_result_deadline = partial(
                compute_result_deadline,
                watchdog_hours=settings.wedap_result_watchdog_hours,
                buffer_minutes=settings.wedap_result_buffer_minutes,
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
    if (
        settings.env not in ("local", "test")
        and settings.db_url.startswith("sqlite")
        and not settings.allow_sqlite_db
    ):
        raise RuntimeError(
            "GW_DB_URL 必须在非 local/test 环境配置真实数据库"
            "（fail-fast，内存 sqlite 会让 readyz 假绿而业务全 500；"
            "仅模拟 dev 分支的单测可显式 GW_ALLOW_SQLITE_DB=true 豁免）"
        )
    # admin caller↔凭证绑定（codex P0）：共享 secret 模式下 X-Caller-Service 可被任何
    # 持 secret 的服务伪造——admin_callers 一旦启用（非空），每个 admin caller 必须在
    # GW_S2S_CALLER_TOKENS 里有专属 token（密码学绑定身份），否则白名单形同虚设。
    # local/test 豁免（无 S2S 场景的单测/本地联调）。
    if settings.env not in ("local", "test"):
        # 与运行时中间件同一解析（parse_caller_tokens）——防两处解析漂移
        # （codex R2 P0：`fund-ops:` 空 token 曾被独立解析误判为已绑定）。
        parsed_tokens = parse_caller_tokens(settings.s2s_caller_tokens) or {}
        # 凭证碰撞 fail-fast（codex R3 P1）：token==共享 secret → 持 secret 者可冒充该
        # caller；两 caller 共用 token → 互相冒充。「专属 token = 身份」的前提是值唯一。
        if settings.s2s_secret and settings.s2s_secret in parsed_tokens.values():
            raise RuntimeError(
                "GW_S2S_CALLER_TOKENS 存在与共享 GW_S2S_SECRET 相同的 token——"
                "身份绑定失效（持共享 secret 即可冒充该 caller），必须使用独立随机 token"
            )
        if len(set(parsed_tokens.values())) != len(parsed_tokens):
            dup_callers = sorted(
                c for c, t in parsed_tokens.items() if list(parsed_tokens.values()).count(t) > 1
            )
            raise RuntimeError(
                f"GW_S2S_CALLER_TOKENS 存在 token 值重复的 caller {dup_callers}——"
                "共用 token 可互相冒充，每个 caller 必须独立随机 token"
            )
        if settings.admin_callers.strip():
            unbound = {c.strip() for c in settings.admin_callers.split(",") if c.strip()} - set(
                parsed_tokens.keys()
            )
            if unbound:
                raise RuntimeError(
                    f"GW_ADMIN_CALLERS 含未绑定专属 token 的 caller {sorted(unbound)}——"
                    "共享 secret 下 caller 头可伪造，admin 白名单登记面必须 per-service token"
                    "（GW_S2S_CALLER_TOKENS）绑定身份"
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
    # 解析 per-service token（A-m-002）：`caller:token,...`；空串 = 不启用。
    # 混合语义：在表内的 caller 强制专属 token，不在表内的回退共享 secret（s2s.py）。
    caller_tokens = parse_caller_tokens(settings.s2s_caller_tokens)

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
        exempt_paths={"/healthz", "/readyz", "/build-info", "/api/version"},
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
    # §7.2.1 顺序表第 1 步：raw request-target 预算先于认证与路由裁决，
    # 故装在 S2S 之外；又在 Identifier 之内，使 414 响应仍带受控 id 与 no-store。
    app.add_middleware(RequestTargetLimitMiddleware)
    app.add_middleware(IdentifierMiddleware)
    engine = build_engine(
        settings.db_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    # 半配置 fail-fast（codex M2 + 兜底评审加固）：投递开启时三类误配启动期拦截，
    # 不留到运行期投递才暴露。判定锚定 URL path 段（urlsplit().path，防 lending-gw
    # 出现在主机名里的假阳性）；/lending-gw 是 gw-internal 银行路由前缀（wedap 路由契约），
    # flow-import 必须走 /external/web2-core。
    if settings.wedap_delivery_enabled:
        base_is_bank_route = (
            urlsplit(settings.wedap_base_url).path.rstrip("/").endswith("/lending-gw")
        )
        import_base = settings.wedap_import_base_url
        # ① base 走银行路由而 import base 未配 → 回落拼成 /lending-gw/bank/api/v1/import/*
        #   误投 wedap-adapter
        if base_is_bank_route and not import_base:
            raise RuntimeError(
                "GW_WEDAP_BASE_URL routes via gw-internal (/lending-gw) and delivery is "
                "enabled — GW_WEDAP_IMPORT_BASE_URL (/external/web2-core route) must be "
                "set explicitly, or flow-import requests would be misrouted to wedap-adapter."
            )
        if import_base:
            # ② import base 也误配成银行路由（照抄 base 的值）→ 同一误路由换入口复现
            if urlsplit(import_base).path.rstrip("/").endswith("/lending-gw"):
                raise RuntimeError(
                    "GW_WEDAP_IMPORT_BASE_URL points at the gw-internal bank route "
                    "(/lending-gw) — flow-import must use the /external/web2-core route."
                )
            # ③ apikey 空 → web2-core 应用层必拒，运行期 401 重试打满才暴露
            if not settings.wedap_import_api_key:
                raise RuntimeError(
                    "GW_WEDAP_IMPORT_BASE_URL is set with delivery enabled but "
                    "GW_WEDAP_IMPORT_API_KEY is empty — web2-core app-layer auth would "
                    "reject every notify/presign at runtime."
                )
        # ④ recon 回调 HMAC secret 空 → 客户端静默发 64 个 0 占位签名，recon 侧验签
        #   持续 401（fail-open）。strip()：该字段不在 _empty_env_as_none validator
        #   白名单，纯空白 "   " 是 truthy 但同样签不出合法签名。local/test 豁免。
        if (
            settings.env not in ("local", "test")
            and not settings.recon_callback_hmac_secret.strip()
        ):
            raise RuntimeError(
                "GW_RECON_CALLBACK_HMAC_SECRET 必须在非 local/test 环境且投递开启时配置"
                "（recon 回调签名 fail-fast，资金网关禁 fail-open：空值时客户端静默发占位签名）"
            )
    app.state.wedap = WedapClient(
        base_url=settings.wedap_base_url,
        timeout_seconds=settings.wedap_timeout_seconds,
        import_api_key=settings.wedap_import_api_key,
        import_base_url=settings.wedap_import_base_url,
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
    app.include_router(platform_accounts_router)
    app.include_router(recon_notify_router)
    app.include_router(deposit_router)
    app.include_router(wedap_import_enqueue_router)
    return app


app = create_app()
