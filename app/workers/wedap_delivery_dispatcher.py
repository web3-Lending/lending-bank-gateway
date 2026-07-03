"""wedap flow-import 投递 dispatcher worker（§4.3「A+异步回执」· gateway 侧）。

make_deliver 把 deliver_task 绑定 S3/wedap 客户端 + bucket 配置为 dispatch 的 deliver 闭包；
run_forever 供 lifespan 以 asyncio.create_task 启动（标 pragma: no cover，逻辑由
dispatch_delivery_once / deliver_task 单测覆盖）。
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.recon_callback import ReconCallbackClient
from app.clients.s3 import S3FileClient
from app.clients.wedap import WedapClient
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.services.wedap_delivery import (
    alert_stuck_deliveries,
    collect_results_once,
    deliver_task,
    dispatch_delivery_once,
    mark_callback_sent,
    resend_pending_callbacks_once,
)
from app.services.wedap_import import CHANNEL_ID
from app.services.wedap_import_result import ImportResult, build_result_key

logger = logging.getLogger(__name__)


def make_on_terminal(
    recon_client: ReconCallbackClient,
    factory: async_sessionmaker[AsyncSession],
) -> Callable[[WedapImportDeliveryTask, str, str | None], Awaitable[None]]:
    """绑定 recon 回执客户端 → dispatch on_terminal 闭包（FU-WEDAP-CALLBACK-DURABLE）。

    回执成功 → mark_callback_sent 标记送达；失败 → 告警不抛、callback_sent_at 留空，由
    resend_pending_callbacks_once 重发兜底，消除 recon 永久卡 ENQUEUED。
    """

    async def _on_terminal(task: WedapImportDeliveryTask, status: str, error: str | None) -> None:
        try:
            await recon_client.post_result(
                tenant_id=task.tenant_id,
                import_batch_no=task.import_batch_no,
                status=status,
                error=error,
            )
        except Exception as exc:  # noqa: BLE001 - 回执失败不抛，留 callback_sent_at 空待重发
            logger.warning(
                "wedap_delivery: 回执 recon 失败 batch=%s status=%s err=%s（待重发）",
                task.import_batch_no,
                status,
                exc,
            )
            return
        await mark_callback_sent(factory, task.id, dt.datetime.now(dt.UTC))

    return _on_terminal


def make_deliver(
    s3_client: S3FileClient,
    wedap_client: WedapClient,
    *,
    staging_bucket: str,
    wedap_bucket: str,
    presigned_enabled: bool = False,
) -> Callable[[WedapImportDeliveryTask], Awaitable[dict[str, Any] | None]]:
    """把 deliver_task 绑定客户端 + bucket → dispatch_delivery_once 的 deliver 闭包。

    presigned_enabled=True 时 wedap 侧上传走 presigned PUT（无需长期 S3 凭证）；False（默认）
    走 boto3 直传，现网行为不变。返回 wedap notify 响应体（§6.1 护栏②：dispatch 落
    accepted_at / result_file_path 留证）。
    """

    async def _deliver(task: WedapImportDeliveryTask) -> dict[str, Any] | None:
        return await deliver_task(
            task,
            s3_client=s3_client,
            wedap_client=wedap_client,
            staging_bucket=staging_bucket,
            wedap_bucket=wedap_bucket,
            presigned_enabled=presigned_enabled,
        )

    return _deliver


_ResultFetch = Callable[[WedapImportDeliveryTask], Awaitable[bytes | None]]
_ResultPost = Callable[[WedapImportDeliveryTask, ImportResult], Awaitable[None]]
# recon 入口 LineResultItem.line_status 的 Literal 四值（ADR-0001 §北向回收：recon 已扩
# CONTRACT_INVALID，见 lending-recon 0039）；非此集的行不投 recon（防非法枚举 422 死循环）。
_VALID_LINE_STATUS = ("INGESTED", "DUPLICATE", "LINE_PARSE_ERROR", "CONTRACT_INVALID")


def make_collect(
    s3_client: S3FileClient,
    recon_client: ReconCallbackClient,
    *,
    wedap_bucket: str,
    wedap_client: WedapClient | None = None,
    presigned_enabled: bool = False,
) -> tuple[_ResultFetch, _ResultPost]:
    """绑定 S3(拉 _result.json) + recon(转投 line-results) → collect_results_once 的 fetch/post。

    fetch 二选一（ADR-0001 P4）：
      - presigned_enabled=False（默认）：boto3 直读 ``wedap_bucket`` + build_result_key；
        NoSuchKey/NotFound/404 = 未就绪 → None（需 wedap S3 凭证）。
      - presigned_enabled=True：向 web2-core 申请 presigned GET URL 再 HTTP GET；presigned GET
        404 = 未就绪 → None（与 boto3 语义一致），需 wedap_client（不传则 raise）。
    post: 逐条异常行（DUPLICATE/LINE_PARSE_ERROR/CONTRACT_INVALID）转投 recon；
          全 INGESTED 则跳过不发。
    """
    if presigned_enabled and wedap_client is None:
        raise ValueError("make_collect: presigned_enabled=True 需传 wedap_client")
    # 仅 presigned 模式绑定 presign client（否则 None）；_fetch 以 `is not None` 二分走 presigned
    # / boto3，免去闭包内 assert（S101）。boto3 模式即便误传 wedap_client 也不用它。
    presign_client = wedap_client if presigned_enabled else None

    async def _fetch(task: WedapImportDeliveryTask) -> bytes | None:
        if presign_client is not None:
            url = await presign_client.request_presign(
                operation="RESULT",
                data_type=task.data_type,
                channel_id=CHANNEL_ID,
                import_date=task.import_date,
                import_batch_no=task.import_batch_no,
            )
            try:
                return await asyncio.to_thread(s3_client.get_bytes_via_presigned_get, url=url)
            except httpx.HTTPStatusError as exc:
                # presigned GET 404 = _result.json 未就绪（wedap 仍在处理）→ None（与 boto3 一致）；
                # 其余（403 越权/URL 过期 / 5xx）为真错，抛。
                if exc.response.status_code == 404:
                    return None
                raise
        key = build_result_key(
            data_type=task.data_type,
            import_date=task.import_date,
            import_batch_no=task.import_batch_no,
        )
        try:
            return await asyncio.to_thread(s3_client.get_bytes, bucket=wedap_bucket, key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code"))
            # 对象不存在=未就绪（不同 S3 兼容实现返回码不一）；其余(NoSuchBucket/AccessDenied)为真错
            if code in ("NoSuchKey", "NotFound", "404"):
                return None  # _result.json 未就绪（wedap 仍在处理）
            raise

    async def _post(task: WedapImportDeliveryTask, result: ImportResult) -> None:
        # 只投 recon 能消化的行：lineNo 是真 int + lineStatus 是四值枚举。parse_result 原样取
        # lineNo，wedap 发 null/{}/"bad"/5.5/bool 等非 int 坏值都会触发 recon line_no:int / Literal
        # 422 → 释放锁永久重试，故此处按 recon contract 完整预校验跳过 + 告警；批级回映留 Phase 2
        # （bool 是 int 子类须显式排除；codex HIGH-3 + 二/三轮）。
        recordable = [
            b
            for b in result.bad_lines
            if isinstance(b.line_no, int)
            and not isinstance(b.line_no, bool)
            and b.line_status in _VALID_LINE_STATUS
        ]
        skipped = len(result.bad_lines) - len(recordable)
        if skipped:
            logger.warning(
                "wedap_delivery: batch=%s 有 %d 条异常行(非法 lineNo / 非法 lineStatus)跳过回传",
                task.import_batch_no,
                skipped,
            )
        if not recordable:  # 全 INGESTED 或全不可消化 → 无可逐行记
            return
        # 规范化 recon LineResultItem 全字段：parse_result 原样取 wedap 值、类型注解不生效，坏
        # 类型/超长会让整批 recon 422 永久重试。error_code 非 str→null·截 recon max_length=64，
        # error_message 非 str→null，dedup_key 非 dict→null（codex 四轮 HIGH，堵死字段级毒丸）。
        await recon_client.post_line_results(
            tenant_id=task.tenant_id,
            import_batch_no=task.import_batch_no,
            data_type=task.data_type,
            line_results=[
                {
                    "line_no": b.line_no,
                    "line_status": b.line_status,
                    "error_code": b.error_code[:64] if isinstance(b.error_code, str) else None,
                    "error_message": b.error_message if isinstance(b.error_message, str) else None,
                    "dedup_key": b.dedup_key if isinstance(b.dedup_key, dict) else None,
                }
                for b in recordable
            ],
        )

    return _fetch, _post


async def run_forever(  # pragma: no cover
    factory: async_sessionmaker[AsyncSession],
    *,
    deliver: Callable[[WedapImportDeliveryTask], Awaitable[dict[str, Any] | None]],
    max_attempts: int,
    interval_seconds: float = 5.0,
    on_terminal: Callable[[WedapImportDeliveryTask, str, str | None], Awaitable[None]]
    | None = None,
    result_fetch: _ResultFetch | None = None,
    result_post: _ResultPost | None = None,
    result_deadline: Callable[[dt.datetime], dt.datetime] | None = None,
    pending_max_age_seconds: float | None = None,
    alert_batch_limit: int = 100,
) -> None:
    """无限循环投递 wedap 任务，每轮间隔 interval_seconds 秒（lifespan 后台 task）。"""
    while True:
        await dispatch_delivery_once(
            factory,
            deliver=deliver,
            now=dt.datetime.now(dt.UTC),
            max_attempts=max_attempts,
            on_terminal=on_terminal,
            result_deadline=result_deadline,
        )
        # durable 回执：重发终态但未送达的回执（FU-WEDAP-CALLBACK-DURABLE）
        if on_terminal is not None:
            await resend_pending_callbacks_once(
                factory, send=on_terminal, now=dt.datetime.now(dt.UTC)
            )
        # result 回收：拉 wedap 写回的 _result.json → 转投 recon line-results（result 回收 Phase1）
        if result_fetch is not None and result_post is not None:
            await collect_results_once(
                factory,
                fetch=result_fetch,
                post=result_post,
                now=dt.datetime.now(dt.UTC),
            )
        # §6.1 护栏③④：PENDING_STUCK / RESULT_OVERDUE 去重告警
        if pending_max_age_seconds is not None:
            await alert_stuck_deliveries(
                factory,
                now=dt.datetime.now(dt.UTC),
                pending_max_age_seconds=pending_max_age_seconds,
                batch_limit=alert_batch_limit,
            )
        await asyncio.sleep(interval_seconds)
