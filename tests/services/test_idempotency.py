from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.services.idempotency import (
    IdempotencyInFlight,
    IdempotencyPayloadMismatch,
    check_or_register,
    payload_hash,
    record_response,
)


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
async def test_registered_but_no_response_raises_in_flight(session) -> None:
    """已注册但 first_response 尚未写入（处理中）→ 应抛 IdempotencyInFlight。

    语义：同一请求正在被处理，调用方应返回 PROCESSING/查询状态，禁止重新执行业务逻辑。
    """
    # 第一次注册（返回 None，正常执行业务逻辑中）
    await check_or_register(
        session,
        tenant_id="t1",
        business_scope="p2p_disburse",
        idempotency_key="inflight_key",
        method="POST",
        path="/p",
        payload=PAYLOAD,
    )
    # 此时 first_response 还未写入 → 重复请求应抛 IdempotencyInFlight
    with pytest.raises(IdempotencyInFlight):
        await check_or_register(
            session,
            tenant_id="t1",
            business_scope="p2p_disburse",
            idempotency_key="inflight_key",
            method="POST",
            path="/p",
            payload=PAYLOAD,
        )


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
    with pytest.raises(IdempotencyPayloadMismatch):
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
    # t2 用同一 key + 不同 payload，不应抛 IdempotencyPayloadMismatch
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


# ---------------------------------------------------------------------------
# 修复 1：并发同键 savepoint + IntegrityError 兜底路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_insert_integrity_error_falls_back_to_for_update() -> None:
    """模拟并发场景：SELECT 返回 None 后 INSERT 抛 IntegrityError（对手已抢先插入）。

    实现方式：对 AsyncSession 完全 mock，精确控制每次 execute / begin_nested 的行为：
    - 第 1 次 execute（初始 SELECT）：scalar_one_or_none() 返回 None（快照隔离看不到对手行）
    - begin_nested：__aexit__ 抛 IntegrityError（模拟并发 INSERT 唯一键冲突）
    - 第 2 次 execute（FOR UPDATE 兜底）：scalar_one_or_none() 返回对手行

    断言：兜底路径返回对手 first_response 而不抛异常。
    """
    from app.models.idempotency import IdempotencyRecord
    from app.services.idempotency import payload_hash as _hash

    h = _hash(PAYLOAD)
    opponent_row = IdempotencyRecord(
        tenant_id="t1",
        business_scope="scope",
        idempotency_key="concurrent_key",
        method="POST",
        path="/p",
        payload_hash=h,
    )
    opponent_row.first_response = {"txnStatus": "OPPONENT"}

    execute_call_count = 0

    async def fake_execute(_stmt, *_a, **_kw):
        nonlocal execute_call_count
        execute_call_count += 1

        class _Result:
            def __init__(self, val):
                self._val = val

            def scalar_one_or_none(self):
                return self._val

        if execute_call_count == 1:
            return _Result(None)  # 初始 SELECT：看不到行
        return _Result(opponent_row)  # FOR UPDATE 兜底：返回对手行

    session = MagicMock()
    session.execute = fake_execute
    session.add = MagicMock()

    # begin_nested：__aexit__ 抛 IntegrityError
    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)

    async def nested_aexit(_self, exc_type, exc_val, exc_tb):
        raise IntegrityError("unique constraint", {}, Exception("UNIQUE constraint failed"))

    nested_cm.__aexit__ = nested_aexit
    session.begin_nested = MagicMock(return_value=nested_cm)

    result = await check_or_register(
        session,
        tenant_id="t1",
        business_scope="scope",
        idempotency_key="concurrent_key",
        method="POST",
        path="/p",
        payload=PAYLOAD,
    )

    # 兜底路径：FOR UPDATE 查到对手行，payload_hash 一致，first_response 非 None → 返回
    assert result == {"txnStatus": "OPPONENT"}
    assert execute_call_count == 2  # 第1次 SELECT（None）+ 第2次 FOR UPDATE 兜底


@pytest.mark.asyncio
async def test_concurrent_insert_fallback_opponent_inflight_raises() -> None:
    """并发兜底路径：对手行 payload_hash 相同但 first_response 为 None → 抛 IdempotencyInFlight。

    场景：两个相同请求并发提交，赢者已插入 row 但尚未写入 first_response；
    败者 FOR UPDATE 查到该行，payload 一致但处理还未完成 → 应抛 IdempotencyInFlight。
    """
    from app.models.idempotency import IdempotencyRecord
    from app.services.idempotency import payload_hash as _hash

    h = _hash(PAYLOAD)
    opponent_row = IdempotencyRecord(
        tenant_id="t1",
        business_scope="scope",
        idempotency_key="concurrent_inflight_key",
        method="POST",
        path="/p",
        payload_hash=h,
    )
    opponent_row.first_response = None  # 对手还未写 first_response

    execute_call_count = 0

    async def fake_execute(_stmt, *_a, **_kw):
        nonlocal execute_call_count
        execute_call_count += 1

        class _Result:
            def __init__(self, val):
                self._val = val

            def scalar_one_or_none(self):
                return self._val

        if execute_call_count == 1:
            return _Result(None)
        return _Result(opponent_row)

    session = MagicMock()
    session.execute = fake_execute
    session.add = MagicMock()

    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)

    async def nested_aexit(_self, exc_type, exc_val, exc_tb):
        raise IntegrityError("unique constraint", {}, Exception("UNIQUE constraint failed"))

    nested_cm.__aexit__ = nested_aexit
    session.begin_nested = MagicMock(return_value=nested_cm)

    with pytest.raises(IdempotencyInFlight):
        await check_or_register(
            session,
            tenant_id="t1",
            business_scope="scope",
            idempotency_key="concurrent_inflight_key",
            method="POST",
            path="/p",
            payload=PAYLOAD,
        )


@pytest.mark.asyncio
async def test_concurrent_conflict_on_different_payload_raises() -> None:
    """并发兜底路径：对手行 payload_hash 不同 → 仍抛 IdempotencyPayloadMismatch。

    FOR UPDATE 查到的对手行持有不同 payload_hash，应抛 IdempotencyPayloadMismatch
    （422，v2.2 §9.1）。
    """
    from app.models.idempotency import IdempotencyRecord
    from app.services.idempotency import payload_hash as _hash

    different_payload = {**PAYLOAD, "amount": "999.0000"}
    opponent_row = IdempotencyRecord(
        tenant_id="t1",
        business_scope="scope",
        idempotency_key="concurrent_conflict_key",
        method="POST",
        path="/p",
        payload_hash=_hash(different_payload),  # 对手用了不同 payload
    )
    opponent_row.first_response = None

    execute_call_count = 0

    async def fake_execute(_stmt, *_a, **_kw):
        nonlocal execute_call_count
        execute_call_count += 1

        class _Result:
            def __init__(self, val):
                self._val = val

            def scalar_one_or_none(self):
                return self._val

        if execute_call_count == 1:
            return _Result(None)
        return _Result(opponent_row)

    session = MagicMock()
    session.execute = fake_execute
    session.add = MagicMock()

    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)

    async def nested_aexit(_self, exc_type, exc_val, exc_tb):
        raise IntegrityError("unique constraint", {}, Exception("UNIQUE constraint failed"))

    nested_cm.__aexit__ = nested_aexit
    session.begin_nested = MagicMock(return_value=nested_cm)

    with pytest.raises(IdempotencyPayloadMismatch):
        await check_or_register(
            session,
            tenant_id="t1",
            business_scope="scope",
            idempotency_key="concurrent_conflict_key",
            method="POST",
            path="/p",
            payload=PAYLOAD,  # 与对手行 hash 不同 → 422
        )


# ---------------------------------------------------------------------------
# 修复 2：payload_hash Decimal 类型语义钉死
# ---------------------------------------------------------------------------


def test_payload_hash_decimal_equals_string() -> None:
    """Decimal('100.0000') 与 str '100.0000' 的 hash 必须相同。

    调用方用 Pydantic .model_dump(mode='json') 时金额是 str；
    若误传 Decimal，_json_default 确保行为一致。
    """
    h_decimal = payload_hash({"amount": Decimal("100.0000")})
    h_string = payload_hash({"amount": "100.0000"})
    assert h_decimal == h_string


def test_payload_hash_float_differs_from_string() -> None:
    """float 100.0 与 str '100.0000' 的 hash 必须不同。

    防止调用方误传 float 金额而被误判为"相同请求"。
    float 经 json.dumps 序列化为 '100.0'，与 str '100.0000' 不同。
    """
    h_float = payload_hash({"amount": 100.0})
    h_string = payload_hash({"amount": "100.0000"})
    assert h_float != h_string


def test_payload_hash_non_serializable_object_falls_back_to_str() -> None:
    """_json_default 对非 Decimal 不可序列化对象兜底为 str()。

    覆盖 _json_default 的 else 分支：保证任何不可序列化对象也能稳定 hash，
    不会抛 TypeError 导致请求失败。
    """

    class _CustomId:
        def __str__(self) -> str:
            return "custom-42"

    h1 = payload_hash({"id": _CustomId()})
    h2 = payload_hash({"id": "custom-42"})
    # str() 兜底后与字符串 "custom-42" 的 hash 一致
    assert h1 == h2


# ---------------------------------------------------------------------------
# 修复 3：record_response 对未找到记录抛 ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_response_raises_value_error_for_missing_key(session) -> None:
    """record_response 对不存在的 key 必须抛 ValueError，不能静默 AttributeError 或 500。"""
    with pytest.raises(ValueError, match="IdempotencyRecord not found"):
        await record_response(
            session,
            tenant_id="t1",
            business_scope="scope",
            idempotency_key="nonexistent_key",
            response={"txnStatus": "DONE"},
        )
