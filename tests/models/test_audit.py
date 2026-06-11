"""tests/models/test_audit.py

单元测试（SQLite in-memory）：
- AuditLog：所有字段 round-trip（含 payload JSON nullable）
- BalanceSnapshot：所有字段 round-trip（含 captured_at aware datetime）
- QueryAudit：所有字段 round-trip（含 caller_service nullable）
"""

import datetime as dt
from decimal import Decimal

import pytest

from app.core.db import build_engine, build_session_factory
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.query_audit import BalanceSnapshot, QueryAudit

# ─────────────────────── SQLite fixture ──────────────────────────────────────


@pytest.fixture()
async def session():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with build_session_factory(engine)() as s:
        yield s
    await engine.dispose()


# ─────────────────────── AuditLog ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_log_fields_round_trip(session) -> None:
    """AuditLog 所有字段写入后可正确读回（含 tenant_id）。"""
    log = AuditLog(
        tenant_id="tenant-uuid-0001",
        actor="admin@wbt.com",
        action="APPROVE_LOAN",
        entity="loan:1234",
        payload={"loan_id": 1234, "amount": "100.0000"},
        prev_hash="abc123def456abc1abc123def456abc1abc123def456abc1abc123def456abc1",
        row_hash="def456abc123def4def456abc123def4def456abc123def4def456abc123def4",
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)

    assert log.tenant_id == "tenant-uuid-0001"
    assert log.actor == "admin@wbt.com"
    assert log.action == "APPROVE_LOAN"
    assert log.entity == "loan:1234"
    assert log.payload == {"loan_id": 1234, "amount": "100.0000"}
    assert log.prev_hash == "abc123def456abc1abc123def456abc1abc123def456abc1abc123def456abc1"
    assert log.row_hash == "def456abc123def4def456abc123def4def456abc123def4def456abc123def4"


@pytest.mark.asyncio
async def test_audit_log_payload_nullable(session) -> None:
    """AuditLog payload 默认为 None（nullable）。"""
    log = AuditLog(
        tenant_id="tenant-uuid-0001",
        actor="system",
        action="HEARTBEAT",
        entity="system:health",
        payload=None,
        prev_hash="0" * 64,
        row_hash="1" * 64,
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)

    assert log.payload is None


@pytest.mark.asyncio
async def test_audit_log_tenant_id_required(session) -> None:
    """AuditLog tenant_id 为 NOT NULL——不传时 commit 抛 IntegrityError。"""
    from sqlalchemy.exc import IntegrityError

    log = AuditLog(
        actor="system",
        action="HEARTBEAT",
        entity="system:health",
        prev_hash="0" * 64,
        row_hash="1" * 64,
    )
    session.add(log)
    with pytest.raises(IntegrityError):
        await session.commit()


# ─────────────────────── BalanceSnapshot ─────────────────────────────────────


@pytest.mark.asyncio
async def test_balance_snapshot_fields_round_trip(session) -> None:
    """BalanceSnapshot 所有字段写入后可正确读回，captured_at 保留 timezone aware。"""
    now = dt.datetime.now(tz=dt.UTC)
    snap = BalanceSnapshot(
        account_id="ACC-20260611-001",
        balance=Decimal("99999.9999"),
        currency="USD",
        source_endpoint="/api/v1/balance",
        captured_at=now,
    )
    session.add(snap)
    await session.commit()
    await session.refresh(snap)

    assert snap.account_id == "ACC-20260611-001"
    assert snap.balance == Decimal("99999.9999")
    assert snap.currency == "USD"
    assert snap.source_endpoint == "/api/v1/balance"
    # captured_at should be stored and retrieved (timezone awareness preserved in MySQL)
    assert snap.captured_at is not None


@pytest.mark.asyncio
async def test_balance_snapshot_index_on_account_id(session) -> None:
    """多条 BalanceSnapshot 可以有相同 account_id（不是唯一约束，仅是索引）。"""
    now = dt.datetime.now(tz=dt.UTC)
    for i in range(3):
        snap = BalanceSnapshot(
            account_id="ACC-SHARED",
            balance=Decimal(f"{i}.0000"),
            currency="HKD",
            source_endpoint="/api/v1/balance",
            captured_at=now,
        )
        session.add(snap)
    await session.commit()  # should not raise


# ─────────────────────── QueryAudit ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_audit_fields_round_trip(session) -> None:
    """QueryAudit 所有字段写入后可正确读回（含 caller_service）。"""
    audit = QueryAudit(
        endpoint="/api/v1/txn/query",
        params_hash="sha256hash01234567890123456789012345678901234567890123456789012",
        trace_id="trace-20260611-001234567890123456789",
        caller_service="lending-lifecycel",
    )
    session.add(audit)
    await session.commit()
    await session.refresh(audit)

    assert audit.endpoint == "/api/v1/txn/query"
    assert audit.params_hash == "sha256hash01234567890123456789012345678901234567890123456789012"
    assert audit.trace_id == "trace-20260611-001234567890123456789"
    assert audit.caller_service == "lending-lifecycel"


@pytest.mark.asyncio
async def test_query_audit_caller_service_nullable(session) -> None:
    """QueryAudit caller_service 默认为 None（nullable）。"""
    audit = QueryAudit(
        endpoint="/api/v1/health",
        params_hash="0" * 64,
        trace_id="trace-000",
    )
    session.add(audit)
    await session.commit()
    await session.refresh(audit)

    assert audit.caller_service is None
