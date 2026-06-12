"""fiat-vault/transactions 供数端点测试（M5 收尾 + T21 inflow/outflow 聚合拆分）。

覆盖：
1. 按账户+窗口查中：items 字段全断言 + aggregate.totalAmount/inflowAmount/outflowAmount 精度
2. payer 侧命中（账户做付方也算）
3. 窗口外日期不命中；txn_date 为 NULL 的 leg 不命中
4. 跨 tenant 隔离（B 租户查不到 A 的）
5. limit 截断 + limit 越界 400 + 日期格式错 400 + dateFrom>dateTo 400
6. 空结果：items=[] aggregate.count=0 totalAmount/inflowAmount/outflowAmount="0.0000"
7. 缺 X-Tenant-Id 400
8. T21 双向流水聚合：inflow/outflow/total 各自正确（含双向种数据）
"""

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.main import create_app
from app.models.base import Base
from app.models.txn import BankTxnLeg, BankTxnOrder

HEADERS = {
    "X-Caller-Service": "recon",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "fv-req-001",
}


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    return TestClient(app)


async def _create_tables(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _insert_order(engine: Any, *, tenant_id: str, biz_seq_no: str) -> int:
    """插入 BankTxnOrder，返回 id，满足 BankTxnLeg FK 约束。"""
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(BankTxnOrder)
            .values(
                tenant_id=tenant_id,
                biz_seq_no=biz_seq_no,
                business_action="DEPOSIT",
                biz_type="FIAT",
                amount=Decimal("100.0000"),
                currency="USD",
                caller_service="recon",
                status="SETTLED",
            )
            .returning(BankTxnOrder.id)
        )
        row = result.fetchone()
        assert row is not None
        return int(row[0])


async def _insert_leg(
    engine: Any,
    *,
    tenant_id: str,
    order_id: int,
    biz_seq_no: str,
    external_ref: str,
    step_type: str,
    step_seq: int,
    amount: str,
    currency: str = "USD",
    payer_account: str | None = None,
    payee_account: str | None = None,
    status: str = "SETTLED",
    txn_date: str | None = "20260611",
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            insert(BankTxnLeg).values(
                tenant_id=tenant_id,
                order_id=order_id,
                biz_seq_no=biz_seq_no,
                external_ref=external_ref,
                step_type=step_type,
                step_seq=step_seq,
                amount=Decimal(amount),
                currency=currency,
                payer_account=payer_account,
                payee_account=payee_account,
                status=status,
                txn_date=txn_date,
            )
        )


# ---------------------------------------------------------------------------
# 1. 基本查中：items 字段全断言 + aggregate totalAmount
# ---------------------------------------------------------------------------


def test_basic_hit_payee(client: TestClient) -> None:
    """payee_account 命中：items 字段全断言，totalAmount="60.0000"。"""
    engine = client.app.state.engine  # type: ignore[union-attr]
    order_id = asyncio.run(_insert_order(engine, tenant_id="OCBC", biz_seq_no="BSQ-VAULT-001"))
    asyncio.run(
        _insert_leg(
            engine,
            tenant_id="OCBC",
            order_id=order_id,
            biz_seq_no="BSQ-VAULT-001",
            external_ref="EXT-001",
            step_type="DEPOSIT",
            step_seq=1,
            amount="40.0000",
            payer_account="PAYER01",
            payee_account="VAULT01",
            txn_date="20260611",
        )
    )
    asyncio.run(
        _insert_leg(
            engine,
            tenant_id="OCBC",
            order_id=order_id,
            biz_seq_no="BSQ-VAULT-001",
            external_ref="EXT-002",
            step_type="FEE",
            step_seq=2,
            amount="20.0000",
            payer_account="PAYER01",
            payee_account="VAULT01",
            txn_date="20260611",
        )
    )

    r = client.get(
        "/api/v1/fiat-vault/transactions",
        params={"accountNo": "VAULT01", "dateFrom": "20260601", "dateTo": "20260630"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    items = body["data"]["items"]
    assert len(items) == 2

    # 全字段断言第一条
    leg1 = items[0]
    assert leg1["bizSeqNo"] == "BSQ-VAULT-001"
    assert leg1["externalRef"] == "EXT-001"
    assert leg1["stepType"] == "DEPOSIT"
    assert leg1["amount"] == "40.0000"
    assert leg1["currency"] == "USD"
    assert leg1["payer"] == "PAYER01"
    assert leg1["payee"] == "VAULT01"
    assert leg1["status"] == "SETTLED"
    assert leg1["txnDate"] == "20260611"

    agg = body["data"]["aggregate"]
    assert agg["count"] == 2
    assert agg["totalAmount"] == "60.0000"
    # 两条 leg 均为 payee=VAULT01，inflow=60，outflow=0
    assert agg["inflowAmount"] == "60.0000"
    assert agg["outflowAmount"] == "0.0000"


# ---------------------------------------------------------------------------
# 2. payer 侧命中
# ---------------------------------------------------------------------------


def test_payer_side_hit(client: TestClient) -> None:
    """payer_account 命中（账户做付方也计入）。"""
    engine = client.app.state.engine  # type: ignore[union-attr]
    order_id = asyncio.run(_insert_order(engine, tenant_id="OCBC", biz_seq_no="BSQ-VAULT-002"))
    asyncio.run(
        _insert_leg(
            engine,
            tenant_id="OCBC",
            order_id=order_id,
            biz_seq_no="BSQ-VAULT-002",
            external_ref="EXT-003",
            step_type="WITHDRAWAL",
            step_seq=1,
            amount="30.0000",
            payer_account="VAULT01",
            payee_account="USER01",
            txn_date="20260611",
        )
    )

    r = client.get(
        "/api/v1/fiat-vault/transactions",
        params={"accountNo": "VAULT01", "dateFrom": "20260601", "dateTo": "20260630"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    items = r.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["payer"] == "VAULT01"
    assert items[0]["amount"] == "30.0000"


# ---------------------------------------------------------------------------
# 3. 窗口外 + txn_date NULL 不命中
# ---------------------------------------------------------------------------


def test_out_of_window_and_null_txn_date_excluded(client: TestClient) -> None:
    """窗口外日期不命中；txn_date 为 NULL 的 leg 也不命中。"""
    engine = client.app.state.engine  # type: ignore[union-attr]
    order_id = asyncio.run(_insert_order(engine, tenant_id="OCBC", biz_seq_no="BSQ-VAULT-003"))
    # 窗口外（20260701 > 20260630）
    asyncio.run(
        _insert_leg(
            engine,
            tenant_id="OCBC",
            order_id=order_id,
            biz_seq_no="BSQ-VAULT-003",
            external_ref="EXT-OUTWIN",
            step_type="DEPOSIT",
            step_seq=1,
            amount="50.0000",
            payee_account="VAULT01",
            txn_date="20260701",
        )
    )
    # txn_date = NULL
    asyncio.run(
        _insert_leg(
            engine,
            tenant_id="OCBC",
            order_id=order_id,
            biz_seq_no="BSQ-VAULT-003",
            external_ref="EXT-NULL",
            step_type="DEPOSIT",
            step_seq=2,
            amount="10.0000",
            payee_account="VAULT01",
            txn_date=None,
        )
    )

    r = client.get(
        "/api/v1/fiat-vault/transactions",
        params={"accountNo": "VAULT01", "dateFrom": "20260601", "dateTo": "20260630"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["items"] == []
    assert data["aggregate"]["count"] == 0
    assert data["aggregate"]["totalAmount"] == "0.0000"
    assert data["aggregate"]["inflowAmount"] == "0.0000"
    assert data["aggregate"]["outflowAmount"] == "0.0000"


# ---------------------------------------------------------------------------
# 4. 跨 tenant 隔离
# ---------------------------------------------------------------------------


def test_cross_tenant_isolation(client: TestClient) -> None:
    """B 租户的 leg 不会出现在 A 租户的查询结果中。"""
    engine = client.app.state.engine  # type: ignore[union-attr]

    # OCBC tenant 的 leg
    ocbc_order = asyncio.run(_insert_order(engine, tenant_id="OCBC", biz_seq_no="BSQ-OCBC-001"))
    asyncio.run(
        _insert_leg(
            engine,
            tenant_id="OCBC",
            order_id=ocbc_order,
            biz_seq_no="BSQ-OCBC-001",
            external_ref="EXT-OCBC-001",
            step_type="DEPOSIT",
            step_seq=1,
            amount="100.0000",
            payee_account="VAULT01",
            txn_date="20260611",
        )
    )

    # DBS tenant 的 leg（同 accountNo）
    dbs_order = asyncio.run(_insert_order(engine, tenant_id="DBS", biz_seq_no="BSQ-DBS-001"))
    asyncio.run(
        _insert_leg(
            engine,
            tenant_id="DBS",
            order_id=dbs_order,
            biz_seq_no="BSQ-DBS-001",
            external_ref="EXT-DBS-001",
            step_type="DEPOSIT",
            step_seq=1,
            amount="999.0000",
            payee_account="VAULT01",
            txn_date="20260611",
        )
    )

    # OCBC 查询只能看到自己的 1 条
    r = client.get(
        "/api/v1/fiat-vault/transactions",
        params={"accountNo": "VAULT01", "dateFrom": "20260601", "dateTo": "20260630"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    items = r.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["amount"] == "100.0000"


# ---------------------------------------------------------------------------
# 5. limit 截断 + limit 越界 400 + 日期格式错 400 + dateFrom>dateTo 400
# ---------------------------------------------------------------------------


def test_limit_truncation(client: TestClient) -> None:
    """limit=1 只返回 1 条（按 id 排序取第一条）。"""
    engine = client.app.state.engine  # type: ignore[union-attr]
    order_id = asyncio.run(_insert_order(engine, tenant_id="OCBC", biz_seq_no="BSQ-LIMIT-001"))
    for i in range(3):
        asyncio.run(
            _insert_leg(
                engine,
                tenant_id="OCBC",
                order_id=order_id,
                biz_seq_no="BSQ-LIMIT-001",
                external_ref=f"EXT-LIM-{i:03d}",
                step_type="DEPOSIT",
                step_seq=i + 1,
                amount="10.0000",
                payee_account="VAULT01",
                txn_date="20260611",
            )
        )

    r = client.get(
        "/api/v1/fiat-vault/transactions",
        params={"accountNo": "VAULT01", "dateFrom": "20260601", "dateTo": "20260630", "limit": 1},
        headers=HEADERS,
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data["items"]) == 1
    assert data["aggregate"]["count"] == 1


def test_limit_zero_400(client: TestClient) -> None:
    """limit=0 → 400 GW_400_VALIDATION。"""
    r = client.get(
        "/api/v1/fiat-vault/transactions",
        params={"accountNo": "VAULT01", "dateFrom": "20260601", "dateTo": "20260630", "limit": 0},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_limit_over_max_400(client: TestClient) -> None:
    """limit=1001 超过 _MAX_LIMIT → 400 GW_400_VALIDATION。"""
    r = client.get(
        "/api/v1/fiat-vault/transactions",
        params={
            "accountNo": "VAULT01",
            "dateFrom": "20260601",
            "dateTo": "20260630",
            "limit": 1001,
        },
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_date_format_error_400(client: TestClient) -> None:
    """日期格式错（非 8 位数字）→ 400 GW_400_VALIDATION。"""
    r = client.get(
        "/api/v1/fiat-vault/transactions",
        params={"accountNo": "VAULT01", "dateFrom": "2026-06-01", "dateTo": "20260630"},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_date_from_gt_date_to_400(client: TestClient) -> None:
    """dateFrom > dateTo → 400 GW_400_VALIDATION。"""
    r = client.get(
        "/api/v1/fiat-vault/transactions",
        params={"accountNo": "VAULT01", "dateFrom": "20260701", "dateTo": "20260601"},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


# ---------------------------------------------------------------------------
# 6. 空结果
# ---------------------------------------------------------------------------


def test_empty_result(client: TestClient) -> None:
    """无匹配数据：items=[], aggregate.count=0, totalAmount="0.0000"。"""
    r = client.get(
        "/api/v1/fiat-vault/transactions",
        params={"accountNo": "NONEXISTENT", "dateFrom": "20260601", "dateTo": "20260630"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["items"] == []
    assert data["aggregate"]["count"] == 0
    assert data["aggregate"]["totalAmount"] == "0.0000"
    assert data["aggregate"]["inflowAmount"] == "0.0000"
    assert data["aggregate"]["outflowAmount"] == "0.0000"


# ---------------------------------------------------------------------------
# 7. 缺 X-Tenant-Id → 400
# ---------------------------------------------------------------------------


def test_missing_tenant_id_400(client: TestClient) -> None:
    """缺 X-Tenant-Id → 400 GW_400_HEADER。"""
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.get(
        "/api/v1/fiat-vault/transactions",
        params={"accountNo": "VAULT01", "dateFrom": "20260601", "dateTo": "20260630"},
        headers=h,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_HEADER"


# ---------------------------------------------------------------------------
# 8. T21 双向流水聚合：inflow/outflow/total 各自正确
# ---------------------------------------------------------------------------


def test_bidirectional_aggregate(client: TestClient) -> None:
    """双向流水：VAULT01 作 payee（inflow）和 payer（outflow）各有一条，三字段断言正确。

    inflow  = 100.0000（VAULT01 为 payee 的 leg）
    outflow = 40.0000 （VAULT01 为 payer 的 leg）
    total   = 140.0000（所有命中 leg 之和，含双向）
    """
    engine = client.app.state.engine  # type: ignore[union-attr]

    # 一条 inflow：VAULT01 收款
    order_in = asyncio.run(_insert_order(engine, tenant_id="OCBC", biz_seq_no="BSQ-BIDIR-001"))
    asyncio.run(
        _insert_leg(
            engine,
            tenant_id="OCBC",
            order_id=order_in,
            biz_seq_no="BSQ-BIDIR-001",
            external_ref="EXT-BIDIR-IN",
            step_type="DEPOSIT",
            step_seq=1,
            amount="100.0000",
            payer_account="EXTERNAL01",
            payee_account="VAULT01",
            txn_date="20260611",
        )
    )

    # 一条 outflow：VAULT01 付款
    order_out = asyncio.run(_insert_order(engine, tenant_id="OCBC", biz_seq_no="BSQ-BIDIR-002"))
    asyncio.run(
        _insert_leg(
            engine,
            tenant_id="OCBC",
            order_id=order_out,
            biz_seq_no="BSQ-BIDIR-002",
            external_ref="EXT-BIDIR-OUT",
            step_type="WITHDRAWAL",
            step_seq=1,
            amount="40.0000",
            payer_account="VAULT01",
            payee_account="EXTERNAL02",
            txn_date="20260611",
        )
    )

    r = client.get(
        "/api/v1/fiat-vault/transactions",
        params={"accountNo": "VAULT01", "dateFrom": "20260601", "dateTo": "20260630"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    agg = r.json()["data"]["aggregate"]
    assert agg["count"] == 2
    assert agg["totalAmount"] == "140.0000"
    assert agg["inflowAmount"] == "100.0000"
    assert agg["outflowAmount"] == "40.0000"
