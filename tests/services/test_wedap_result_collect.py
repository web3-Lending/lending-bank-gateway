"""collect_results_once 单测（result 回收 Phase1 · 注入 fetch/post + 内存 sqlite）。

覆盖：就绪→post 转投 + result_collected_at 置位；未就绪(None)→不 post/不置位/释放锁；
幂等(已回收不再 post)；只扫 DELIVERED；fetch 异常→释放锁不置位(单条失败不崩)。
"""

import datetime as dt
import json
from unittest.mock import AsyncMock

import pytest

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.services.wedap_delivery import collect_results_once
from app.services.wedap_import_result import ImportResult

_NOW = dt.datetime(2026, 6, 30, 16, 0, tzinfo=dt.UTC)

# wedap _result.json：1 INGESTED + 1 DUPLICATE + 1 LINE_PARSE_ERROR → bad_lines 收 2 条
_RESULT_JSON = json.dumps(
    {
        "importStatus": "PARTIAL",
        "ingestedCount": 1,
        "duplicateCount": 1,
        "lineErrorCount": 1,
        "lineResults": [
            {"lineNo": 1, "lineStatus": "INGESTED"},
            {
                "lineNo": 2,
                "lineStatus": "DUPLICATE",
                "dedupKey": {"loanId": "L1", "asOfDate": "20260630"},
            },
            {"lineNo": 3, "lineStatus": "LINE_PARSE_ERROR", "errorCode": "INVALID_DATE_FORMAT"},
        ],
    }
).encode()


@pytest.fixture()
async def factory(tmp_path):
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


async def _insert(
    factory,
    *,
    status="DELIVERED",
    result_collected_at=None,
    result_locked_at=None,
    batch="BATCH-LEN-20260630-001",
):
    async with factory() as s:
        s.add(
            WedapImportDeliveryTask(
                tenant_id="WBTHK01",
                request_id=f"wedap-import-{batch}",
                import_batch_no=batch,
                data_type="loan-detail",
                import_date="20260630",
                staging_key="staging/k.jsonl",
                file_checksum="a" * 64,
                file_size=10,
                total_count=3,
                status=status,
                result_collected_at=result_collected_at,
                result_locked_at=result_locked_at,
            )
        )
        await s.commit()


async def _get(factory, batch="BATCH-LEN-20260630-001"):
    async with factory() as s:
        return (
            await s.execute(
                WedapImportDeliveryTask.__table__.select().where(
                    WedapImportDeliveryTask.import_batch_no == batch
                )
            )
        ).first()


@pytest.mark.asyncio
async def test_collect_ready_posts_and_marks_collected(factory):
    await _insert(factory)
    fetch = AsyncMock(return_value=_RESULT_JSON)
    post = AsyncMock()

    n = await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)

    assert n == 1
    post.assert_awaited_once()
    posted: ImportResult = post.await_args.args[1]
    assert len(posted.bad_lines) == 2  # INGESTED 不进 bad_lines
    row = await _get(factory)
    assert row.result_collected_at is not None
    assert row.result_locked_at is None  # 置位时清锁


@pytest.mark.asyncio
async def test_collect_not_ready_releases_lock_without_marking(factory):
    await _insert(factory)
    fetch = AsyncMock(return_value=None)  # _result.json 未就绪
    post = AsyncMock()

    n = await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)

    assert n == 0
    post.assert_not_awaited()
    row = await _get(factory)
    assert row.result_collected_at is None  # 未置位 → 下轮重试
    assert row.result_locked_at is None  # 锁已释放


@pytest.mark.asyncio
async def test_collect_idempotent_skips_already_collected(factory):
    await _insert(factory, result_collected_at=_NOW)
    fetch = AsyncMock(return_value=_RESULT_JSON)
    post = AsyncMock()

    n = await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)

    assert n == 0
    fetch.assert_not_awaited()
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_only_scans_delivered(factory):
    await _insert(factory, status="PENDING", batch="B-PENDING")
    await _insert(factory, status="FAILED", batch="B-FAILED")
    fetch = AsyncMock(return_value=_RESULT_JSON)
    post = AsyncMock()

    n = await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)

    assert n == 0
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_fetch_error_releases_lock_no_crash(factory):
    await _insert(factory)
    fetch = AsyncMock(side_effect=RuntimeError("s3 down"))
    post = AsyncMock()

    n = await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)

    assert n == 0  # 单条失败被吞，不崩循环
    post.assert_not_awaited()
    row = await _get(factory)
    assert row.result_collected_at is None
    assert row.result_locked_at is None  # 锁释放，待下轮重试
