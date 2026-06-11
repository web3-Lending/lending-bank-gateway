"""outbox_dispatcher worker 测试：验证 run_forever 循环调用 dispatch_once 后等待间隔。"""

import asyncio
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_run_forever_calls_dispatch_once_then_sleep() -> None:
    """run_forever：调用 dispatch_once 后 sleep，取消后干净退出。"""
    from app.workers.outbox_dispatcher import run_forever

    call_count = 0

    async def fake_dispatch_once(*_args, **_kwargs) -> int:  # noqa: ANN002, ANN003
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError
        return 1

    fake_factory = object()
    targets = {"lifecycle": "http://lifecycle/cb"}

    with patch("app.workers.outbox_dispatcher.dispatch_once", side_effect=fake_dispatch_once):
        with pytest.raises(asyncio.CancelledError):
            await run_forever(
                fake_factory,  # type: ignore[arg-type]
                targets=targets,
                max_attempts=3,
                interval_seconds=0,
            )

    assert call_count >= 1
