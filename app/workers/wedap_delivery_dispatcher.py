"""wedap flow-import 投递 dispatcher worker（§4.3「A+异步回执」· gateway 侧）。

make_deliver 把 deliver_task 绑定 S3/wedap 客户端 + bucket 配置为 dispatch 的 deliver 闭包；
run_forever 供 lifespan 以 asyncio.create_task 启动（标 pragma: no cover，逻辑由
dispatch_delivery_once / deliver_task 单测覆盖）。
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.recon_callback import ReconCallbackClient
from app.clients.s3 import S3FileClient
from app.clients.wedap import WedapClient
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.services.wedap_delivery import (
    deliver_task,
    dispatch_delivery_once,
    mark_callback_sent,
    resend_pending_callbacks_once,
)

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
) -> Callable[[WedapImportDeliveryTask], Awaitable[None]]:
    """把 deliver_task 绑定客户端 + bucket → dispatch_delivery_once 的 deliver 闭包。"""

    async def _deliver(task: WedapImportDeliveryTask) -> None:
        await deliver_task(
            task,
            s3_client=s3_client,
            wedap_client=wedap_client,
            staging_bucket=staging_bucket,
            wedap_bucket=wedap_bucket,
        )

    return _deliver


async def run_forever(  # pragma: no cover
    factory: async_sessionmaker[AsyncSession],
    *,
    deliver: Callable[[WedapImportDeliveryTask], Awaitable[None]],
    max_attempts: int,
    interval_seconds: float = 5.0,
    on_terminal: Callable[[WedapImportDeliveryTask, str, str | None], Awaitable[None]]
    | None = None,
) -> None:
    """无限循环投递 wedap 任务，每轮间隔 interval_seconds 秒（lifespan 后台 task）。"""
    while True:
        await dispatch_delivery_once(
            factory,
            deliver=deliver,
            now=dt.datetime.now(dt.UTC),
            max_attempts=max_attempts,
            on_terminal=on_terminal,
        )
        # durable 回执：重发终态但未送达的回执（FU-WEDAP-CALLBACK-DURABLE）
        if on_terminal is not None:
            await resend_pending_callbacks_once(factory, send=on_terminal)
        await asyncio.sleep(interval_seconds)
