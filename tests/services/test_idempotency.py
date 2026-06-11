import pytest

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.services.idempotency import IdempotencyConflict, check_or_register, record_response


@pytest.fixture()
async def session():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with build_session_factory(engine)() as s:
        yield s
    await engine.dispose()


PAYLOAD = {"bizSeqNo": "DSB-20260611-0000000000001", "amount": "100.0000"}


@pytest.mark.asyncio
async def test_first_call_registers_and_returns_none(session) -> None:
    hit = await check_or_register(
        session,
        tenant_id="t1",
        business_scope="p2p_disburse",
        idempotency_key="DSB-20260611-0000000000001",
        method="POST",
        path="/api/v1/loans/p2p-disbursements",
        payload=PAYLOAD,
    )
    assert hit is None


@pytest.mark.asyncio
async def test_replay_same_payload_returns_first_response(session) -> None:
    await check_or_register(
        session,
        tenant_id="t1",
        business_scope="p2p_disburse",
        idempotency_key="k1",
        method="POST",
        path="/p",
        payload=PAYLOAD,
    )
    await record_response(
        session,
        tenant_id="t1",
        business_scope="p2p_disburse",
        idempotency_key="k1",
        response={"txnStatus": "PROCESSING"},
        final_effect_id="order:1",
    )
    hit = await check_or_register(
        session,
        tenant_id="t1",
        business_scope="p2p_disburse",
        idempotency_key="k1",
        method="POST",
        path="/p",
        payload=PAYLOAD,
    )
    assert hit == {"txnStatus": "PROCESSING"}


@pytest.mark.asyncio
async def test_same_key_different_payload_conflicts(session) -> None:
    await check_or_register(
        session,
        tenant_id="t1",
        business_scope="p2p_disburse",
        idempotency_key="k1",
        method="POST",
        path="/p",
        payload=PAYLOAD,
    )
    with pytest.raises(IdempotencyConflict):
        await check_or_register(
            session,
            tenant_id="t1",
            business_scope="p2p_disburse",
            idempotency_key="k1",
            method="POST",
            path="/p",
            payload={**PAYLOAD, "amount": "999.0000"},
        )


@pytest.mark.asyncio
async def test_different_tenant_same_key_no_conflict(session) -> None:
    """不同 tenant 用同一 key —— 互相不冲突。"""
    await check_or_register(
        session,
        tenant_id="t1",
        business_scope="p2p_disburse",
        idempotency_key="shared_key",
        method="POST",
        path="/p",
        payload=PAYLOAD,
    )
    # t2 用同一 key + 不同 payload，不应抛 IdempotencyConflict
    hit = await check_or_register(
        session,
        tenant_id="t2",
        business_scope="p2p_disburse",
        idempotency_key="shared_key",
        method="POST",
        path="/p",
        payload={**PAYLOAD, "amount": "999.0000"},
    )
    assert hit is None


@pytest.mark.asyncio
async def test_payload_hash_order_insensitive(session) -> None:
    """payload_hash 对 dict 键顺序不敏感——乱序后 hash 应一致，返回同一响应。"""
    await check_or_register(
        session,
        tenant_id="t1",
        business_scope="p2p_disburse",
        idempotency_key="k_order",
        method="POST",
        path="/p",
        payload={"amount": "100.0000", "bizSeqNo": "DSB-20260611-0000000000001"},
    )
    await record_response(
        session,
        tenant_id="t1",
        business_scope="p2p_disburse",
        idempotency_key="k_order",
        response={"txnStatus": "ACCEPTED"},
    )
    # 键顺序打乱
    hit = await check_or_register(
        session,
        tenant_id="t1",
        business_scope="p2p_disburse",
        idempotency_key="k_order",
        method="POST",
        path="/p",
        payload={"bizSeqNo": "DSB-20260611-0000000000001", "amount": "100.0000"},
    )
    assert hit == {"txnStatus": "ACCEPTED"}
