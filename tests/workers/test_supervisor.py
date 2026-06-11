"""supervisor 监督循环测试。"""

import asyncio

import pytest


@pytest.mark.asyncio
async def test_supervisor_restarts_after_exception(caplog: pytest.LogCaptureFixture) -> None:
    """factory 第一次抛 RuntimeError，第二次调用时取消任务，验证：
    - exception 日志被记录（第一次崩溃）
    - factory 被调用超过一次（重启发生）
    """
    from app.workers.supervisor import supervised

    call_count = 0

    async def flaky_factory() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        # 第二次：正常运行中阻塞，等待外部 cancel
        await asyncio.sleep(10)

    task = asyncio.create_task(supervised("test-worker", flaky_factory, restart_delay_seconds=0))

    # 给 event loop 足够的 yield 让 factory 跑两次（异常 + 重启进入第二次）
    for _ in range(10):
        await asyncio.sleep(0)
        if call_count >= 2:
            break

    # 此时记录日志应该已经发生
    with caplog.at_level("ERROR", logger="app.workers.supervisor"):
        pass  # caplog 已在 test 函数级收集，此处仅确认 level 设置

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count >= 2, f"expected >= 2 calls, got {call_count}"
    assert any("test-worker" in r.message and "crashed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_supervisor_propagates_cancelled_error() -> None:
    """CancelledError 透传不重启：task.cancel() 后 supervisor task 干净结束。"""
    from app.workers.supervisor import supervised

    call_count = 0

    async def cancellable_factory() -> None:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(10)  # 阻塞等待，供 cancel 打断

    task = asyncio.create_task(
        supervised("test-cancel-worker", cancellable_factory, restart_delay_seconds=0)
    )
    # 让 factory 跑起来
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # factory 只被调了一次（CancelledError 不触发重启）
    assert call_count == 1
