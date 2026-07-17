"""app/workers/recon_worker.py

摄取 worker：扫 NOTIFIED 任务 → 原子 claim DOWNLOADING → 下载校验 → DOWNLOADED → 解析落库 PARSED。

状态枚举（完整）：
  NOTIFIED    — 已收到通知，等待 worker 处理
  DOWNLOADING — 已被某 worker 原子 claim，正在下载（并发安全）
  DOWNLOADED  — 下载完成，等待解析
  PARSED      — 解析落库完成
  FAILED      — 下载或解析失败
  SUPERSEDED  — 被同 task_no 更高版本覆盖

run_forever 为薄壳循环，pragma: no cover；核心逻辑在 ingest_pending_once。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.s3 import S3FileClient
from app.models.recon import ReconResultTask
from app.services.recon_ingest import ColumnDrift, DataQualityError, parse_and_land

logger = logging.getLogger(__name__)


async def _set_status(
    factory: async_sessionmaker[AsyncSession],
    task_id: int,
    status: str,
    extra: dict[str, object] | None = None,
) -> None:
    """在独立事务里通过 UPDATE 更新 task 状态（无 if-branch，coverage 友好）。"""
    values: dict[str, object] = {"status": status}
    if extra:
        values.update(extra)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(ReconResultTask).where(ReconResultTask.id == task_id).values(**values)
            )


async def _atomic_claim(
    factory: async_sessionmaker[AsyncSession],
    task_id: int,
) -> bool:
    """原子 claim：UPDATE ... SET status='DOWNLOADING' WHERE id=:id AND status='NOTIFIED'。

    返回 True 表示 claim 成功（rowcount==1）；False 表示已被他人 claim，调用方应 skip。
    """
    async with factory() as session:
        async with session.begin():
            result = await session.execute(
                update(ReconResultTask)
                .where(
                    ReconResultTask.id == task_id,
                    ReconResultTask.status == "NOTIFIED",
                )
                .values(status="DOWNLOADING")
            )
    return cast(bool, result.rowcount == 1)  # type: ignore[attr-defined]


async def ingest_pending_once(
    factory: async_sessionmaker[AsyncSession],
    *,
    s3: S3FileClient,
    archive_dir: str,
) -> int:
    """扫 NOTIFIED → 原子 claim DOWNLOADING → to_thread 下载校验 → DOWNLOADED → 解析落库 PARSED。

    单笔失败（Md5Mismatch/ColumnDrift/S3 异常/其它异常）置 FAILED 不阻塞其它 task；
    worker 上下文无 trace（X-Trace-Id 语义不适用，日志以 task_no/version 关联）。

    返回本轮处理的 task 数量（含失败；被他人 claim 的不计入）。
    """
    async with factory() as session:
        tasks = (
            (
                await session.execute(
                    select(ReconResultTask).where(ReconResultTask.status == "NOTIFIED")
                )
            )
            .scalars()
            .all()
        )
        snapshot = [(t.id, t.s3_bucket, t.s3_key, t.file_md5, t.task_no, t.version) for t in tasks]

    handled = 0
    for tid, bucket, key, md5, task_no, version in snapshot:
        # ── 原子 claim：防止多 worker 并发重复处理 ───────────────────────
        claimed = await _atomic_claim(factory, tid)
        if not claimed:
            logger.warning(
                "recon ingest skip %s v%s: already claimed by another worker",
                task_no,
                version,
            )
            continue

        dest = f"{archive_dir}/{task_no}_v{version}.xlsx"
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        # ── 下载 + MD5 校验 ──────────────────────────────────────────────
        try:
            await asyncio.to_thread(
                s3.download_verified,
                bucket=bucket,
                key=key,
                expected_md5=md5,
                dest=dest,
            )
        except Exception as exc:  # Md5Mismatch / boto 异常
            logger.exception("recon ingest download failed %s v%s", task_no, version)
            await _set_status(
                factory,
                tid,
                "FAILED",
                {"column_check": {"download_error": type(exc).__name__}},
            )
            handled += 1
            continue

        # ── 标记 DOWNLOADED ──────────────────────────────────────────────
        await _set_status(factory, tid, "DOWNLOADED")

        # ── 解析落库 ─────────────────────────────────────────────────────
        try:
            await parse_and_land(factory, task_id=tid, xlsx_path=dest)
        except (ColumnDrift, DataQualityError) as exc:
            # 二者已由 parse_and_land 自置 FAILED + 明细留痕（drift 信息 / data_error
            # 的 column+value）——worker 层不得再置位，覆盖会把明细冲掉只剩异常名。
            logger.exception(
                "recon ingest parse failed (%s) %s v%s", type(exc).__name__, task_no, version
            )
        except Exception as exc:
            # 其它异常（DataQualityError / RuntimeError / ...）→ 置 FAILED + 留痕
            logger.exception(
                "recon ingest parse failed (%s) %s v%s", type(exc).__name__, task_no, version
            )
            await _set_status(
                factory,
                tid,
                "FAILED",
                {"column_check": {"parse_error": type(exc).__name__}},
            )

        handled += 1

    return handled


async def run_forever(  # pragma: no cover
    factory: async_sessionmaker[AsyncSession],
    *,
    s3: S3FileClient,
    archive_dir: str,
    interval_seconds: float = 10.0,
) -> None:
    """无限循环摄取 NOTIFIED 任务，每轮间隔 interval_seconds 秒。"""
    while True:
        await ingest_pending_once(factory, s3=s3, archive_dir=archive_dir)
        await asyncio.sleep(interval_seconds)
