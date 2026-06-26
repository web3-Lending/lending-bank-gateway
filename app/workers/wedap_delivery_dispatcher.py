"""wedap flow-import 投递 dispatcher worker（§4.3「A+异步回执」· gateway 侧）。

make_deliver 把 deliver_task 绑定 S3/wedap 客户端 + bucket 配置为 dispatch 的 deliver 闭包；
run_forever 供 lifespan 以 asyncio.create_task 启动（标 pragma: no cover，逻辑由
dispatch_delivery_once / deliver_task 单测覆盖）。
"""

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.s3 import S3FileClient
from app.clients.wedap import WedapClient
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.services.wedap_delivery import deliver_task, dispatch_delivery_once


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
) -> None:
    """无限循环投递 wedap 任务，每轮间隔 interval_seconds 秒（lifespan 后台 task）。"""
    while True:
        await dispatch_delivery_once(
            factory,
            deliver=deliver,
            now=dt.datetime.now(dt.UTC),
            max_attempts=max_attempts,
        )
        await asyncio.sleep(interval_seconds)
