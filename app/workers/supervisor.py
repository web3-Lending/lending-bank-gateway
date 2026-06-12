import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def supervised(
    name: str, factory: Callable[[], Awaitable[None]], *, restart_delay_seconds: float = 5.0
) -> None:
    """worker 监督循环：异常 → logger.exception + 退避重启；CancelledError 透传（shutdown）。"""
    while True:
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker %s crashed; restart in %.1fs", name, restart_delay_seconds)
            await asyncio.sleep(restart_delay_seconds)
