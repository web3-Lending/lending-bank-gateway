"""collect_results_once 单测（result 回收 Phase1 · 注入 fetch/post + 内存 sqlite）。

覆盖：就绪→post 转投 + result_collected_at 置位；未就绪(None)→不 post/不置位/释放锁；
幂等(已回收不再 post)；只扫 DELIVERED；fetch 异常→释放锁不置位(单条失败不崩)。
"""

import datetime as dt
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.models.wedap_delivery_alert import WedapDeliveryAlert
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
    result_lock_token=None,
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
                result_lock_token=result_lock_token,
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


@pytest.mark.asyncio
async def test_mark_result_collected_ownership_guard(factory):
    """锁被别的实例重抢(result_lock_token != 本轮 token) → mark no-op 返 False，不冒名置位。"""
    from app.services.wedap_delivery import mark_result_collected

    await _insert(
        factory,
        result_locked_at=dt.datetime(2026, 6, 30, 15, 0, tzinfo=dt.UTC),
        result_lock_token="tok-b",  # noqa: S106
    )
    row = await _get(factory)

    marked = await mark_result_collected(factory, row.id, "tok-a", _NOW)  # token 不匹配

    assert marked is False
    row2 = await _get(factory)
    assert row2.result_collected_at is None  # 没被冒名置位
    assert row2.result_lock_token == "tok-b"  # noqa: S105  别人的锁 token 没被覆盖


@pytest.mark.asyncio
async def test_release_result_lock_ownership_guard(factory):
    """release 只清自己的锁；锁已被别的实例重抢(token 不同)时 no-op，不清掉新 owner。"""
    from app.services.wedap_delivery import _release_result_lock

    await _insert(
        factory,
        result_locked_at=dt.datetime(2026, 6, 30, 15, 0, tzinfo=dt.UTC),
        result_lock_token="tok-b",  # noqa: S106
    )
    row = await _get(factory)

    await _release_result_lock(factory, row.id, "tok-a")  # token 不匹配

    row2 = await _get(factory)
    assert row2.result_locked_at is not None  # 别人的锁没被清掉
    assert row2.result_lock_token == "tok-b"  # noqa: S105


# wedap watchdog 超时批：整批 FAILED、lineResults 空、带文件级 fileErrorCode
_FAILED_RESULT_JSON = json.dumps(
    {
        "importStatus": "FAILED",
        "ingestedCount": 0,
        "duplicateCount": 0,
        "lineErrorCount": 0,
        "contractInvalidCount": 0,
        "fileErrorCode": "PROCESSING_TIMEOUT",
        "fileErrorMsg": "Batch did not complete before deadline",
        "lineResults": [],
    }
).encode()

_SUCCESS_RESULT_JSON = json.dumps(
    {
        "importStatus": "SUCCESS",
        "ingestedCount": 3,
        "duplicateCount": 0,
        "lineErrorCount": 0,
        "lineResults": [{"lineNo": 1, "lineStatus": "INGESTED"}],
    }
).encode()

# 行聚合 FAILED：全行被拒（无 INGESTED），有坏行明细 → 真 _post 会投 recon（可因 recon 故障抛错）
_FAILED_ROWAGG_RESULT_JSON = json.dumps(
    {
        "importStatus": "FAILED",
        "ingestedCount": 0,
        "duplicateCount": 0,
        "lineErrorCount": 2,
        "lineResults": [
            {"lineNo": 1, "lineStatus": "LINE_PARSE_ERROR", "errorCode": "INVALID_JSON"},
            {"lineNo": 2, "lineStatus": "CONTRACT_INVALID", "errorCode": "CONTRACT_SHAPE_MISMATCH"},
        ],
    }
).encode()


async def _get_alerts(factory, kind=None):
    async with factory() as s:
        rows = (await s.execute(WedapDeliveryAlert.__table__.select())).all()
    return [r for r in rows if kind is None or r.kind == kind]


@pytest.mark.asyncio
async def test_collect_failed_result_records_import_failed_alert(factory):
    # 护栏⑥：整批 FAILED（watchdog 超时，bad_lines 空 → 转投 recon 无逐行信号）→ 发 IMPORT_FAILED
    # 告警，否则批级失败静默无人知。仍标已回收（结果到了，不误触 RESULT_OVERDUE）。
    await _insert(factory)
    fetch = AsyncMock(return_value=_FAILED_RESULT_JSON)
    post = AsyncMock()

    n = await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)

    assert n == 1
    row = await _get(factory)
    assert row.result_collected_at is not None  # FAILED 仍标已回收，不误触 RESULT_OVERDUE
    alerts = await _get_alerts(factory, kind="IMPORT_FAILED")
    assert len(alerts) == 1
    assert alerts[0].import_batch_no == "BATCH-LEN-20260630-001"
    assert alerts[0].tenant_id == "WBTHK01"
    assert "PROCESSING_TIMEOUT" in alerts[0].detail  # 失败原因带进 detail 供人工判


@pytest.mark.asyncio
async def test_collect_success_result_no_import_failed_alert(factory):
    await _insert(factory)
    fetch = AsyncMock(return_value=_SUCCESS_RESULT_JSON)
    post = AsyncMock()

    await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)

    assert await _get_alerts(factory, kind="IMPORT_FAILED") == []


@pytest.mark.asyncio
async def test_collect_partial_result_no_import_failed_alert(factory):
    # PARTIAL（部分入库）不是批级失败，坏行已转投 recon，不发 IMPORT_FAILED。
    await _insert(factory)
    fetch = AsyncMock(return_value=_RESULT_JSON)  # _RESULT_JSON 是 PARTIAL
    post = AsyncMock()

    await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)

    assert await _get_alerts(factory, kind="IMPORT_FAILED") == []


@pytest.mark.asyncio
async def test_collect_failed_alert_deduped_on_reprocess(factory):
    # 同批 FAILED 再次回收（锁过期重抢/重放）→ UNIQUE 去重：DB 仍 1 条 + ERROR 只发一次（不刷屏）。
    await _insert(factory)
    fetch = AsyncMock(return_value=_FAILED_RESULT_JSON)
    post = AsyncMock()

    with patch("app.services.wedap_delivery.logger") as mock_logger:
        await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)
        async with factory() as s:  # 复位回收态模拟重处理（锁过期/重放要幂等）
            await s.execute(
                WedapImportDeliveryTask.__table__.update()
                .where(WedapImportDeliveryTask.import_batch_no == "BATCH-LEN-20260630-001")
                .values(result_collected_at=None, result_locked_at=None, result_lock_token=None)
            )
            await s.commit()
        await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)

    assert fetch.await_count == 2  # 第二次确实重新进入处理路径（非跳过=假绿）
    assert post.await_count == 2
    assert len(await _get_alerts(factory, kind="IMPORT_FAILED")) == 1  # DB 去重仍 1 条
    failed_errors = [c for c in mock_logger.error.call_args_list if "import FAILED" in str(c)]
    assert len(failed_errors) == 1  # ERROR 只发一次，去重时不重复刷屏


@pytest.mark.asyncio
async def test_collect_failed_alert_recorded_even_if_post_fails(factory):
    # P1 回归：行聚合 FAILED（有坏行 → 真 _post 会投 recon）时 post 抛异常，告警须已先于 post 落库、
    # 不被 except 吞掉（盲区）；且锁释放待下轮重试。
    await _insert(factory)
    fetch = AsyncMock(return_value=_FAILED_ROWAGG_RESULT_JSON)
    post = AsyncMock(side_effect=RuntimeError("recon down"))

    n = await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)

    assert n == 0  # post 失败 → 未标已回收，下轮重试
    post.assert_awaited_once()  # 告警在 post 之前，post 确被调用后才抛
    row = await _get(factory)
    assert row.result_collected_at is None  # 未标已回收
    assert row.result_locked_at is None  # 锁已释放待重试
    assert row.result_lock_token is None
    assert (
        len(await _get_alerts(factory, kind="IMPORT_FAILED")) == 1
    )  # 告警已独立落库（post 失败也在）


@pytest.mark.asyncio
async def test_mark_result_collected_matching_token_succeeds(factory):
    """token 匹配(正常单实例路径) → mark 成功置位返 True，不受 MySQL 时间戳截微秒影响。"""
    from app.services.wedap_delivery import mark_result_collected

    await _insert(factory, result_locked_at=_NOW, result_lock_token="t-123")  # noqa: S106
    row = await _get(factory)

    marked = await mark_result_collected(factory, row.id, "t-123", _NOW)

    assert marked is True
    row2 = await _get(factory)
    assert row2.result_collected_at is not None
    assert row2.result_lock_token is None  # 置位时清 token


@pytest.mark.asyncio
async def test_collect_skips_when_claim_lost(factory, monkeypatch):
    """scan 到但 claim 竞败(别实例抢先)→ continue 跳过，不拉取。"""
    from app.services import wedap_delivery as _wd

    await _insert(factory)
    monkeypatch.setattr(_wd, "_claim_result", AsyncMock(return_value=False))
    fetch = AsyncMock()
    post = AsyncMock()

    n = await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)

    assert n == 0
    fetch.assert_not_awaited()  # claim 没抢到 → 不拉 _result.json


@pytest.mark.asyncio
async def test_collect_mark_lost_not_counted(factory, monkeypatch):
    """post 成功但 mark 竞败(锁被别实例重抢)→ 不计数(503->483 分支)。"""
    from app.services import wedap_delivery as _wd

    await _insert(factory)
    monkeypatch.setattr(_wd, "mark_result_collected", AsyncMock(return_value=False))
    fetch = AsyncMock(return_value=_RESULT_JSON)
    post = AsyncMock()

    n = await collect_results_once(factory, fetch=fetch, post=post, now=_NOW)

    assert n == 0  # mark 返 False → 不计数
    post.assert_awaited_once()  # post 已调，只是置位竞败
