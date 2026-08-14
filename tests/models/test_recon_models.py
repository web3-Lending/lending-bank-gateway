"""tests/models/test_recon_models.py

单元测试（SQLite in-memory）+ 集成测试（真实 MySQL，标记 integration）。

单元测试覆盖：
- ReconResultTask：两个 UNIQUE 约束（uq_recon_task_req / uq_recon_task_ver）
- ReconResultTask：所有字段 round-trip（含 nullable / JSON）
- ReconResultDiff：字段 round-trip
- ReconResultSourceWedap：字段 round-trip
- ReconResultSourceBank：字段 round-trip

集成测试覆盖（@pytest.mark.integration）：
- MySQL 唯一约束 uq_recon_task_req
- MySQL 唯一约束 uq_recon_task_ver
"""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.recon import (
    ReconResultDiff,
    ReconResultSourceBank,
    ReconResultSourceWedap,
    ReconResultTask,
)

# ─────────────────────── SQLite fixture ──────────────────────────────────────


@pytest.fixture()
async def session():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with build_session_factory(engine)() as s:
        yield s
    await engine.dispose()


# ─────────────────────── helpers ─────────────────────────────────────────────


def _task(**kw) -> ReconResultTask:
    defaults = dict(
        tenant_id="WBTHK01",
        task_no="RECON-20260611-001",
        version=1,
        recon_date="20260611",
        s3_bucket="wbt-recon-bucket",
        s3_key="recon/20260611/result.xlsx",
        file_md5="d41d8cd98f00b204e9800998ecf8427e",
        diff_count=0,
        status="NOTIFIED",
        request_id="REQ-20260611-0001",
    )
    defaults.update(kw)
    return ReconResultTask(**defaults)


_TENANT = "WBTHK01"


# ─────────────────────── ReconResultTask — unique constraints ─────────────────


@pytest.mark.asyncio
async def test_task_uq_recon_task_req(session) -> None:
    """同 (tenant_id, request_id) 重复插入应触发 IntegrityError (uq_recon_task_req)。"""
    session.add(_task())
    await session.commit()
    # 不同 task_no+version，相同 tenant_id+request_id → 撞 uq_recon_task_req
    session.add(_task(task_no="RECON-20260611-002", version=2))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_task_uq_recon_task_ver(session) -> None:
    """同 (tenant_id, task_no, version) 重复插入应触发 IntegrityError (uq_recon_task_ver)。"""
    session.add(_task())
    await session.commit()
    # 不同 request_id，相同 tenant_id+task_no+version → 撞 uq_recon_task_ver
    session.add(_task(request_id="REQ-20260611-0002"))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_task_cross_tenant_no_conflict(session) -> None:
    """不同 tenant_id 相同 request_id + task_no+version 不冲突。"""
    session.add(_task(tenant_id="T1"))
    await session.commit()
    session.add(_task(tenant_id="T2"))
    await session.commit()  # should not raise


# ─────────────────────── ReconResultTask — field round-trip ──────────────────


@pytest.mark.asyncio
async def test_task_status_values(session) -> None:
    """status 字段接受所有合法值：NOTIFIED / DOWNLOADED / PARSED / FAILED / SUPERSEDED。"""
    for i, status in enumerate(["NOTIFIED", "DOWNLOADED", "PARSED", "FAILED", "SUPERSEDED"]):
        session.add(
            _task(
                task_no=f"RECON-20260611-{i:03d}",
                request_id=f"REQ-{i:04d}",
                status=status,
            )
        )
    await session.commit()


@pytest.mark.asyncio
async def test_task_all_fields_round_trip(session) -> None:
    """所有字段（含 nullable / JSON）写入后可正确读回。"""
    task = _task(
        task_no="RECON-20260611-FULL",
        version=3,
        recon_date="20260611",
        s3_bucket="bucket-test",
        s3_key="path/to/key.xlsx",
        file_md5="abc123def456abc1",
        diff_count=42,
        status="PARSED",
        request_id="REQ-FULL-0001",
        parser_version="v1.2.3",
        schema_version="v2.0",
        column_check={"col_a": True, "col_b": False},
        archive_path="archive/path/result.xlsx",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    assert task.task_no == "RECON-20260611-FULL"
    assert task.version == 3
    assert task.recon_date == "20260611"
    assert task.s3_bucket == "bucket-test"
    assert task.s3_key == "path/to/key.xlsx"
    assert task.file_md5 == "abc123def456abc1"
    assert task.diff_count == 42
    assert task.status == "PARSED"
    assert task.request_id == "REQ-FULL-0001"
    assert task.parser_version == "v1.2.3"
    assert task.schema_version == "v2.0"
    assert task.column_check == {"col_a": True, "col_b": False}
    assert task.archive_path == "archive/path/result.xlsx"


@pytest.mark.asyncio
async def test_task_nullable_fields_default_none(session) -> None:
    """parser_version / schema_version / column_check / archive_path 默认为 None。"""
    task = _task()
    session.add(task)
    await session.commit()
    await session.refresh(task)

    assert task.parser_version is None
    assert task.schema_version is None
    assert task.column_check is None
    assert task.archive_path is None


# ─────────────────────── ReconResultDiff — field round-trip ──────────────────


@pytest.mark.asyncio
async def test_diff_fields_round_trip(session) -> None:
    """ReconResultDiff 所有字段（含 nullable）写入后可正确读回。"""
    parent = _task()
    session.add(parent)
    await session.commit()

    diff = ReconResultDiff(
        tenant_id=_TENANT,
        task_id=parent.id,
        diff_type="AMOUNT",
        wedap_biz_seq_no="DSB-20260611-0001",
        bank_seq_no="HSBC202606110001",
        wedap_amount=Decimal("100.0000"),
        bank_amount=Decimal("99.9900"),
        diff_amount=Decimal("0.0100"),
        wedap_status="SUCCESS",
        bank_status="SETTLED",
    )
    session.add(diff)
    await session.commit()
    await session.refresh(diff)

    assert diff.task_id == parent.id
    assert diff.diff_type == "AMOUNT"
    assert diff.wedap_biz_seq_no == "DSB-20260611-0001"
    assert diff.bank_seq_no == "HSBC202606110001"
    assert diff.wedap_amount == Decimal("100.0000")
    assert diff.bank_amount == Decimal("99.9900")
    assert diff.diff_amount == Decimal("0.0100")
    assert diff.wedap_status == "SUCCESS"
    assert diff.bank_status == "SETTLED"


@pytest.mark.asyncio
async def test_diff_nullable_fields_default_none(session) -> None:
    """ReconResultDiff nullable 字段默认 None。"""
    parent = _task()
    session.add(parent)
    await session.commit()

    diff = ReconResultDiff(tenant_id=_TENANT, task_id=parent.id, diff_type="MISSING")
    session.add(diff)
    await session.commit()
    await session.refresh(diff)

    assert diff.wedap_biz_seq_no is None
    assert diff.bank_seq_no is None
    assert diff.wedap_amount is None
    assert diff.bank_amount is None
    assert diff.diff_amount is None
    assert diff.wedap_status is None
    assert diff.bank_status is None


# ─────────────────────── ReconResultSourceWedap — field round-trip ───────────


@pytest.mark.asyncio
async def test_source_wedap_fields_round_trip(session) -> None:
    """ReconResultSourceWedap 所有字段写入后可正确读回。"""
    parent = _task()
    session.add(parent)
    await session.commit()

    row = ReconResultSourceWedap(
        tenant_id=_TENANT,
        task_id=parent.id,
        biz_type="DSB",
        biz_seq_no="DSB-20260611-0001",
        bank_biz_seq_no="HSBC202606110001",
        amount=Decimal("100.0000"),
        currency="USD",
        payer_account="ACC-001",
        payee_account="ACC-002",
        status="SUCCESS",
        error_msg=None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    assert row.task_id == parent.id
    assert row.biz_type == "DSB"
    assert row.biz_seq_no == "DSB-20260611-0001"
    assert row.bank_biz_seq_no == "HSBC202606110001"
    assert row.amount == Decimal("100.0000")
    assert row.currency == "USD"
    assert row.payer_account == "ACC-001"
    assert row.payee_account == "ACC-002"
    assert row.status == "SUCCESS"
    assert row.error_msg is None


@pytest.mark.asyncio
async def test_source_wedap_nullable_defaults(session) -> None:
    """ReconResultSourceWedap nullable 字段默认 None；biz_seq_no NOT NULL。"""
    parent = _task()
    session.add(parent)
    await session.commit()

    row = ReconResultSourceWedap(tenant_id=_TENANT, task_id=parent.id, biz_seq_no="SEQ-001")
    session.add(row)
    await session.commit()
    await session.refresh(row)

    assert row.biz_type is None
    assert row.bank_biz_seq_no is None
    assert row.amount is None
    assert row.currency is None
    assert row.payer_account is None
    assert row.payee_account is None
    assert row.status is None
    assert row.error_msg is None


# ─────────────────────── ReconResultSourceBank — field round-trip ────────────


@pytest.mark.asyncio
async def test_source_bank_fields_round_trip(session) -> None:
    """ReconResultSourceBank 所有字段写入后可正确读回。"""
    parent = _task()
    session.add(parent)
    await session.commit()

    row = ReconResultSourceBank(
        tenant_id=_TENANT,
        task_id=parent.id,
        bank_seq_no="HSBC202606110001",
        txn_date="20260611",
        amount=Decimal("100.0000"),
        currency="USD",
        payer_account="ACC-001",
        payee_account="ACC-002",
        status="SETTLED",
        file_name="recon_20260611.xlsx",
        line_no=5,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    assert row.task_id == parent.id
    assert row.bank_seq_no == "HSBC202606110001"
    assert row.txn_date == "20260611"
    assert row.amount == Decimal("100.0000")
    assert row.currency == "USD"
    assert row.payer_account == "ACC-001"
    assert row.payee_account == "ACC-002"
    assert row.status == "SETTLED"
    assert row.file_name == "recon_20260611.xlsx"
    assert row.line_no == 5


@pytest.mark.asyncio
async def test_source_bank_nullable_defaults(session) -> None:
    """ReconResultSourceBank nullable 字段默认 None；bank_seq_no NOT NULL。"""
    parent = _task()
    session.add(parent)
    await session.commit()

    row = ReconResultSourceBank(tenant_id=_TENANT, task_id=parent.id, bank_seq_no="HSBC001")
    session.add(row)
    await session.commit()
    await session.refresh(row)

    assert row.txn_date is None
    assert row.amount is None
    assert row.currency is None
    assert row.payer_account is None
    assert row.payee_account is None
    assert row.status is None
    assert row.file_name is None
    assert row.line_no is None


# ─────────────────────── schema_version 列长防回归 ───────────────────────────


def test_schema_version_column_fits_constant() -> None:
    """SCHEMA_VERSION 常量长度必须 <= ReconResultTask.schema_version 列长（防截断）。"""
    from app.services.recon_ingest import SCHEMA_VERSION

    col_length: int = ReconResultTask.schema_version.type.length  # type: ignore[union-attr]
    assert len(SCHEMA_VERSION) <= col_length, (
        f"SCHEMA_VERSION ({len(SCHEMA_VERSION)} chars) exceeds "
        f"column length ({col_length}): {SCHEMA_VERSION!r}"
    )


def test_parser_version_column_fits_constant() -> None:
    """PARSER_VERSION 常量长度必须 <= ReconResultTask.parser_version 列长（防截断）。"""
    from app.services.recon_ingest import PARSER_VERSION

    col_length: int = ReconResultTask.parser_version.type.length  # type: ignore[union-attr]
    assert len(PARSER_VERSION) <= col_length, (
        f"PARSER_VERSION ({len(PARSER_VERSION)} chars) exceeds "
        f"column length ({col_length}): {PARSER_VERSION!r}"
    )


# ─────────────────────── Integration tests (MySQL) ───────────────────────────

pytestmark_integration = pytest.mark.integration


@pytest.fixture(scope="module")
def mysql_engine_recon():
    """启动 MySQL 8.0 testcontainer，创建所有表，返回 sync engine。"""
    try:
        from testcontainers.mysql import MySqlContainer  # type: ignore[import-untyped]
    except ImportError:
        pytest.skip("testcontainers 未安装")

    # import 确保模型已注册到 Base.metadata
    import app.models.recon  # noqa: F401
    from app.models.base import Base

    try:
        with MySqlContainer("mysql:8.0").with_command("--innodb-use-native-aio=0") as mysql:
            url = (
                f"mysql+pymysql://{mysql.username}:{mysql.password}"
                f"@{mysql.get_container_host_ip()}:{mysql.get_exposed_port(3306)}"
                f"/{mysql.dbname}"
            )
            import sqlalchemy as sa

            engine = sa.create_engine(url, pool_pre_ping=True)
            Base.metadata.create_all(engine)
            yield engine
            engine.dispose()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"docker/testcontainer 不可用: {exc}")


@pytest.fixture()
def db_session_recon(mysql_engine_recon):
    """每个 integration 测试独立 Session。"""
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=mysql_engine_recon, expire_on_commit=False)
    session = factory()
    yield session
    session.rollback()
    session.close()


@pytest.mark.integration
def test_task_uq_recon_task_req_mysql(db_session_recon) -> None:
    """MySQL：同 (tenant_id, request_id) 应触发 IntegrityError (uq_recon_task_req)。"""
    from app.models.recon import ReconResultTask

    row = dict(
        tenant_id="WBTHK01",
        task_no="RECON-INT-001",
        version=1,
        recon_date="20260611",
        s3_bucket="bucket",
        s3_key="key",
        file_md5="md5",
        diff_count=0,
        status="NOTIFIED",
        request_id="REQ-INT-0001",
    )
    db_session_recon.add(ReconResultTask(**row))
    db_session_recon.flush()
    # 不同 task_no+version，相同 request_id → 撞 uq_recon_task_req
    db_session_recon.add(ReconResultTask(**{**row, "task_no": "RECON-INT-002", "version": 2}))
    with pytest.raises(IntegrityError):
        db_session_recon.flush()


@pytest.mark.integration
def test_task_uq_recon_task_ver_mysql(db_session_recon) -> None:
    """MySQL：同 (tenant_id, task_no, version) 应触发 IntegrityError (uq_recon_task_ver)。"""
    from app.models.recon import ReconResultTask

    row = dict(
        tenant_id="WBTHK01",
        task_no="RECON-INT-VER-001",
        version=1,
        recon_date="20260611",
        s3_bucket="bucket",
        s3_key="key",
        file_md5="md5",
        diff_count=0,
        status="NOTIFIED",
        request_id="REQ-INT-VER-0001",
    )
    db_session_recon.add(ReconResultTask(**row))
    db_session_recon.flush()
    # 不同 request_id，相同 task_no+version → 撞 uq_recon_task_ver
    db_session_recon.add(ReconResultTask(**{**row, "request_id": "REQ-INT-VER-0002"}))
    with pytest.raises(IntegrityError):
        db_session_recon.flush()


# ─────────────────────── G4：复合 FK (task_id, tenant_id) 物理拦截 ──────────────


@pytest.mark.asyncio
async def test_diff_orphan_task_id_raises_fk(session) -> None:
    """G4：子表 task_id 指向不存在 task → 复合 FK 触发 IntegrityError（SQLite FK pragma ON）。"""
    session.add(ReconResultDiff(tenant_id=_TENANT, task_id=999999, diff_type="MISSING"))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_diff_cross_tenant_task_raises_fk(session) -> None:
    """G4：子表 tenant_id 与父 task.tenant_id 不配对 → 复合 FK (task_id,tenant_id) 拦截。"""
    parent = _task(tenant_id=_TENANT)
    session.add(parent)
    await session.commit()
    # task_id 正确但 tenant 不同 → 复合 FK 无匹配父行
    session.add(ReconResultDiff(tenant_id="OTHER-TENANT", task_id=parent.id, diff_type="MISSING"))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_source_wedap_orphan_task_id_raises_fk(session) -> None:
    """G4：source_wedap 孤儿 task_id → 复合 FK 拦截。"""
    session.add(ReconResultSourceWedap(tenant_id=_TENANT, task_id=999999, biz_seq_no="SEQ-X"))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_source_bank_orphan_task_id_raises_fk(session) -> None:
    """G4：source_bank 孤儿 task_id → 复合 FK 拦截。"""
    session.add(ReconResultSourceBank(tenant_id=_TENANT, task_id=999999, bank_seq_no="HSBC-X"))
    with pytest.raises(IntegrityError):
        await session.commit()
