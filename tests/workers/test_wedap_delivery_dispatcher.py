import datetime as dt
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.workers.wedap_delivery_dispatcher import make_collect, make_deliver, make_on_terminal

_CONTENT = b'{"h":1}\n{"loanId":"L1"}\n'
_CHECKSUM = hashlib.sha256(_CONTENT).hexdigest()


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
    callback_sent_at=None,
    callback_locked_at=None,
    batch="BATCH-LEN-20260624-001",
):
    async with factory() as s:
        s.add(
            WedapImportDeliveryTask(
                tenant_id="WBTHK01",
                request_id=f"wedap-import-{batch}",
                import_batch_no=batch,
                data_type="interest-accrual",
                import_date="20260624",
                staging_key="staging/k.jsonl",
                file_checksum=_CHECKSUM,
                file_size=len(_CONTENT),
                total_count=1,
                status=status,
                last_error=None,
                callback_sent_at=callback_sent_at,
                callback_locked_at=callback_locked_at,
            )
        )
        await s.commit()


async def _get(factory, batch="BATCH-LEN-20260624-001"):
    async with factory() as s:
        return (
            await s.execute(
                select(WedapImportDeliveryTask).where(
                    WedapImportDeliveryTask.import_batch_no == batch
                )
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_make_deliver_binds_buckets_and_clients(factory):
    s3 = MagicMock()
    s3.get_bytes = MagicMock(return_value=_CONTENT)
    s3.upload = MagicMock(return_value=_CHECKSUM)
    wedap = AsyncMock()
    wedap.notify_batch_uploaded = AsyncMock(return_value={"status": "ACCEPTED"})

    deliver = make_deliver(s3, wedap, staging_bucket="stg", wedap_bucket="wedap")
    await _insert(factory, status="SENDING")
    await deliver(await _get(factory))

    s3.get_bytes.assert_called_once_with(bucket="stg", key="staging/k.jsonl")
    assert s3.upload.call_args.kwargs["bucket"] == "wedap"
    wedap.notify_batch_uploaded.assert_awaited_once()


@pytest.mark.asyncio
async def test_make_on_terminal_success_marks_callback_sent(factory):
    await _insert(factory, status="DELIVERED", callback_sent_at=None)
    task = await _get(factory)
    recon = AsyncMock()
    recon.post_result = AsyncMock()

    on_terminal = make_on_terminal(recon, factory)
    await on_terminal(task, "DELIVERED", None)

    recon.post_result.assert_awaited_once()
    assert (await _get(factory)).callback_sent_at is not None  # 成功→标记送达


@pytest.mark.asyncio
async def test_make_on_terminal_failure_leaves_callback_unsent(factory):
    await _insert(factory, status="FAILED", callback_sent_at=None)
    task = await _get(factory)
    recon = AsyncMock()
    recon.post_result = AsyncMock(side_effect=RuntimeError("recon down"))

    on_terminal = make_on_terminal(recon, factory)
    await on_terminal(task, "FAILED", "notify 5xx")  # 不抛

    recon.post_result.assert_awaited_once()
    assert (await _get(factory)).callback_sent_at is None  # 失败→留空待重发


@pytest.mark.asyncio
async def test_resend_via_on_terminal_picks_unsent_terminal(factory):
    """resend pass(由 run_forever 调)用 on_terminal 重发终态未送达回执。"""
    from app.services.wedap_delivery import resend_pending_callbacks_once

    await _insert(factory, status="DELIVERED", callback_sent_at=None, batch="B-UNSENT")
    await _insert(
        factory,
        status="DELIVERED",
        callback_sent_at=dt.datetime(2026, 6, 24, tzinfo=dt.UTC),
        batch="B-SENT",
    )
    await _insert(factory, status="PENDING", callback_sent_at=None, batch="B-PENDING")
    recon = AsyncMock()
    recon.post_result = AsyncMock()
    on_terminal = make_on_terminal(recon, factory)

    n = await resend_pending_callbacks_once(factory, send=on_terminal, now=dt.datetime.now(dt.UTC))

    assert n == 1  # 只 B-UNSENT(终态+未送达);B-SENT 已送达跳过,B-PENDING 非终态跳过
    recon.post_result.assert_awaited_once()
    assert (await _get(factory, "B-UNSENT")).callback_sent_at is not None


@pytest.mark.asyncio
async def test_resend_claim_skips_freshly_locked(factory):
    """已被别的实例 claim(callback_locked_at 新鲜)→ resend 跳过,不重发。"""
    from app.services.wedap_delivery import resend_pending_callbacks_once

    now = dt.datetime(2026, 6, 24, 12, 0, tzinfo=dt.UTC)
    await _insert(
        factory,
        status="DELIVERED",
        callback_sent_at=None,
        callback_locked_at=now - dt.timedelta(seconds=10),
        batch="B-LOCKED",
    )
    recon = AsyncMock()
    recon.post_result = AsyncMock()
    n = await resend_pending_callbacks_once(factory, send=make_on_terminal(recon, factory), now=now)
    assert n == 0
    recon.post_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_resend_claim_reclaims_stale_lock(factory):
    """残留锁(超时)→ 可重新 claim 重发。"""
    from app.services.wedap_delivery import resend_pending_callbacks_once

    now = dt.datetime(2026, 6, 24, 12, 0, tzinfo=dt.UTC)
    await _insert(
        factory,
        status="DELIVERED",
        callback_sent_at=None,
        callback_locked_at=now - dt.timedelta(seconds=3600),
        batch="B-STALE",
    )
    recon = AsyncMock()
    recon.post_result = AsyncMock()
    n = await resend_pending_callbacks_once(factory, send=make_on_terminal(recon, factory), now=now)
    assert n == 1
    recon.post_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_resend_skips_when_claim_lost(factory, monkeypatch):
    """scan 到但 claim 竞败(别的实例抢先)→ continue 跳过不重发。"""
    from app.services import wedap_delivery as _wd
    from app.services.wedap_delivery import resend_pending_callbacks_once

    await _insert(factory, status="DELIVERED", callback_sent_at=None, batch="B-RACE")
    monkeypatch.setattr(_wd, "_claim_callback", AsyncMock(return_value=False))
    recon = AsyncMock()
    recon.post_result = AsyncMock()
    n = await resend_pending_callbacks_once(
        factory, send=make_on_terminal(recon, factory), now=dt.datetime(2026, 6, 24, tzinfo=dt.UTC)
    )
    assert n == 0
    recon.post_result.assert_not_awaited()


def _task(batch="BATCH-LEN-20260630-001"):
    return WedapImportDeliveryTask(
        tenant_id="WBTHK01",
        request_id=f"wedap-import-{batch}",
        import_batch_no=batch,
        data_type="loan-detail",
        import_date="20260630",
        staging_key="staging/k.jsonl",
        file_checksum="a" * 64,
        file_size=10,
        total_count=3,
        status="DELIVERED",
    )


@pytest.mark.asyncio
async def test_make_collect_fetch_404_returns_none():
    """_result.json 未就绪(NoSuchKey/404)→ fetch 返 None（不当异常）。"""
    from botocore.exceptions import ClientError

    s3 = MagicMock()
    s3.get_bytes = MagicMock(side_effect=ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject"))
    fetch, _ = make_collect(s3, AsyncMock(), wedap_bucket="wedap")

    assert await fetch(_task()) is None


@pytest.mark.asyncio
async def test_make_collect_fetch_other_error_raises():
    """非 404 的 S3 错误(如 AccessDenied)→ 抛出（不静默吞）。"""
    from botocore.exceptions import ClientError

    s3 = MagicMock()
    s3.get_bytes = MagicMock(
        side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    )
    fetch, _ = make_collect(s3, AsyncMock(), wedap_bucket="wedap")

    with pytest.raises(ClientError):
        await fetch(_task())


@pytest.mark.asyncio
async def test_make_collect_post_maps_bad_lines_to_recon():
    """post 把 bad_lines 映成 line_results 字典转投 recon.post_line_results。"""
    from app.services.wedap_import_result import BadLine, ImportResult

    recon = AsyncMock()
    recon.post_line_results = AsyncMock()
    _, post = make_collect(MagicMock(), recon, wedap_bucket="wedap")
    result = ImportResult(
        import_status="PARTIAL",
        ingested_count=1,
        duplicate_count=1,
        line_error_count=1,
        bad_lines=[
            BadLine(
                line_no=2,
                line_status="DUPLICATE",
                error_message=None,
                dedup_key={"loanId": "L1", "asOfDate": "20260630"},
            ),
            BadLine(
                line_no=3,
                line_status="LINE_PARSE_ERROR",
                error_message="bad date",
                error_code="INVALID_DATE_FORMAT",
            ),
        ],
    )

    await post(_task(), result)

    recon.post_line_results.assert_awaited_once()
    kw = recon.post_line_results.await_args.kwargs
    assert kw["import_batch_no"] == "BATCH-LEN-20260630-001"
    assert kw["data_type"] == "loan-detail"
    assert len(kw["line_results"]) == 2
    assert kw["line_results"][0]["dedup_key"] == {"loanId": "L1", "asOfDate": "20260630"}
    assert kw["line_results"][1]["error_code"] == "INVALID_DATE_FORMAT"


@pytest.mark.asyncio
async def test_make_collect_post_forwards_contract_invalid():
    """ADR-0001 §北向回收：CONTRACT_INVALID 现在转投 recon（旧行为是静默跳过→漏账）。"""
    from app.services.wedap_import_result import BadLine, ImportResult

    recon = AsyncMock()
    recon.post_line_results = AsyncMock()
    _, post = make_collect(MagicMock(), recon, wedap_bucket="wedap")
    result = ImportResult(
        import_status="PARTIAL",
        ingested_count=0,
        duplicate_count=0,
        line_error_count=1,
        bad_lines=[
            BadLine(
                line_no=7,
                line_status="CONTRACT_INVALID",
                error_message="parsed contract rejected",
                error_code="CONTRACT_MISMATCH",
            ),
        ],
    )

    await post(_task(), result)

    recon.post_line_results.assert_awaited_once()
    lines = recon.post_line_results.await_args.kwargs["line_results"]
    assert len(lines) == 1
    assert lines[0]["line_status"] == "CONTRACT_INVALID"
    assert lines[0]["error_code"] == "CONTRACT_MISMATCH"


@pytest.mark.asyncio
async def test_make_collect_post_skips_when_all_ingested():
    """全 INGESTED(无 bad_lines)→ 不发 recon（无异常行可记）。"""
    from app.services.wedap_import_result import ImportResult

    recon = AsyncMock()
    recon.post_line_results = AsyncMock()
    _, post = make_collect(MagicMock(), recon, wedap_bucket="wedap")
    result = ImportResult(
        import_status="SUCCESS",
        ingested_count=3,
        duplicate_count=0,
        line_error_count=0,
        bad_lines=[],
    )

    await post(_task(), result)

    recon.post_line_results.assert_not_awaited()


@pytest.mark.asyncio
async def test_make_collect_post_skips_invalid_line_status():
    """非法 lineStatus 跳过不回传，避免 recon Literal 422 死循环（codex 二轮 MEDIUM-1）。"""
    from app.services.wedap_import_result import BadLine, ImportResult

    recon = AsyncMock()
    recon.post_line_results = AsyncMock()
    _, post = make_collect(MagicMock(), recon, wedap_bucket="wedap")
    result = ImportResult(
        import_status="PARTIAL",
        ingested_count=0,
        duplicate_count=1,
        line_error_count=1,
        bad_lines=[
            BadLine(line_no=2, line_status="None", error_message="missing status"),  # 非法
            BadLine(line_no=3, line_status="duplicate", error_message="大小写漂移"),  # 非法
            BadLine(
                line_no=4, line_status="DUPLICATE", error_message=None, dedup_key={"txnId": "T1"}
            ),  # 合法
        ],
    )

    await post(_task(), result)

    recon.post_line_results.assert_awaited_once()
    lr = recon.post_line_results.await_args.kwargs["line_results"]
    assert len(lr) == 1 and lr[0]["line_no"] == 4  # 只剩合法三值枚举行


@pytest.mark.asyncio
async def test_make_collect_fetch_notfound_returns_none():
    """部分 S3 兼容实现返回 NotFound 而非 NoSuchKey → 也当未就绪 None（codex P1 LOW-1）。"""
    from botocore.exceptions import ClientError

    s3 = MagicMock()
    s3.get_bytes = MagicMock(side_effect=ClientError({"Error": {"Code": "NotFound"}}, "GetObject"))
    fetch, _ = make_collect(s3, AsyncMock(), wedap_bucket="wedap")

    assert await fetch(_task()) is None


@pytest.mark.asyncio
async def test_make_collect_post_skips_null_line_no():
    """null lineNo 行跳过不回传，避免 recon line_no:int 422 死循环（codex P1 HIGH-3）。"""
    from app.services.wedap_import_result import BadLine, ImportResult

    recon = AsyncMock()
    recon.post_line_results = AsyncMock()
    _, post = make_collect(MagicMock(), recon, wedap_bucket="wedap")
    result = ImportResult(
        import_status="PARTIAL",
        ingested_count=0,
        duplicate_count=1,
        line_error_count=1,
        bad_lines=[
            BadLine(
                line_no=None,
                line_status="LINE_PARSE_ERROR",
                error_message="broken",
                error_code="INVALID_JSON",
            ),
            BadLine(
                line_no=5, line_status="DUPLICATE", error_message=None, dedup_key={"txnId": "T1"}
            ),
        ],
    )

    await post(_task(), result)

    recon.post_line_results.assert_awaited_once()
    lr = recon.post_line_results.await_args.kwargs["line_results"]
    assert len(lr) == 1 and lr[0]["line_no"] == 5  # null lineNo 行被剔除


@pytest.mark.asyncio
async def test_make_collect_post_all_null_line_no_skips_send():
    """全为 null lineNo → 无可逐行记，不发 recon（codex P1 HIGH-3）。"""
    from app.services.wedap_import_result import BadLine, ImportResult

    recon = AsyncMock()
    recon.post_line_results = AsyncMock()
    _, post = make_collect(MagicMock(), recon, wedap_bucket="wedap")
    result = ImportResult(
        import_status="FAILED",
        ingested_count=0,
        duplicate_count=0,
        line_error_count=1,
        bad_lines=[
            BadLine(
                line_no=None,
                line_status="LINE_PARSE_ERROR",
                error_message="x",
                error_code="INVALID_JSON",
            ),
        ],
    )

    await post(_task(), result)

    recon.post_line_results.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_line_no", [{}, [], "bad", 5.5, True])
async def test_make_collect_post_skips_non_int_line_no(bad_line_no):
    """lineNo 非 int(dict/list/str/float/bool)→跳过，避免 recon 422（codex 三轮 HIGH）。"""
    from app.services.wedap_import_result import BadLine, ImportResult

    recon = AsyncMock()
    recon.post_line_results = AsyncMock()
    _, post = make_collect(MagicMock(), recon, wedap_bucket="wedap")
    bad = BadLine(
        line_no=bad_line_no,  # type: ignore[arg-type]
        line_status="DUPLICATE",
        error_message=None,
    )
    good = BadLine(line_no=7, line_status="DUPLICATE", error_message=None, dedup_key={"txnId": "T"})
    result = ImportResult(
        import_status="PARTIAL",
        ingested_count=0,
        duplicate_count=2,
        line_error_count=0,
        bad_lines=[bad, good],
    )

    await post(_task(), result)

    recon.post_line_results.assert_awaited_once()
    lr = recon.post_line_results.await_args.kwargs["line_results"]
    assert len(lr) == 1 and lr[0]["line_no"] == 7  # 非 int lineNo 行被剔除


@pytest.mark.asyncio
async def test_make_collect_post_sanitizes_bad_field_types():
    """error_code/message/dedup_key 坏类型/超长被规范化, 不让 recon 422（codex 四轮 HIGH）。"""
    from app.services.wedap_import_result import BadLine, ImportResult

    recon = AsyncMock()
    recon.post_line_results = AsyncMock()
    _, post = make_collect(MagicMock(), recon, wedap_bucket="wedap")
    bad = BadLine(
        line_no=2,
        line_status="DUPLICATE",
        error_message=[],  # type: ignore[arg-type]  非 str
        error_code="X" * 100,  # 超 recon max_length=64
        dedup_key="not-a-dict",  # type: ignore[arg-type]  非 dict
    )
    result = ImportResult(
        import_status="PARTIAL",
        ingested_count=0,
        duplicate_count=1,
        line_error_count=0,
        bad_lines=[bad],
    )

    await post(_task(), result)

    lr = recon.post_line_results.await_args.kwargs["line_results"][0]
    assert lr["error_code"] == "X" * 64  # 截到 64
    assert lr["error_message"] is None  # 非 str→null
    assert lr["dedup_key"] is None  # 非 dict→null
