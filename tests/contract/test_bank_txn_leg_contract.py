"""跨库直读契约（gateway 侧）—— A-M-004。

ADR-0031/README：lending-recon 通过跨库 collector **直读** gateway 库的 `bank_txn_leg` 物理表
（Plan 3），绕过 HTTP 供数路径。该物理表因此成为跨服务契约，但物理表本身无版本化保护——
gateway 改列名/类型/可空性会让 recon 侧静默错账且无 CI 信号。

本测试把 `bank_txn_leg` 的公开列形态（列名 + python 类型 + 可空性）冻结为契约快照。任何改动
→ 本测试 red，强制改动者同步评估 recon 跨库 collector 影响、协同更新（recon 侧契约测试 +
迁移）后再更新此快照。recon 仓侧的对账契约测试为独立 followup（需跨仓协作）。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.models.txn import BankTxnLeg

# 冻结契约：列名 -> (python 类型, nullable)
_EXPECTED_BANK_TXN_LEG_CONTRACT: dict[str, tuple[type, bool]] = {
    "id": (int, False),
    "tenant_id": (str, False),
    "order_id": (int, False),
    "biz_seq_no": (str, False),
    "external_system": (str, False),
    "external_ref": (str, False),
    "step_type": (str, False),
    "step_seq": (int, False),
    "amount": (Decimal, False),
    "currency": (str, False),
    "payer_account": (str, True),
    "payee_account": (str, True),
    "status": (str, False),
    "txn_date": (str, True),
    "posted_at": (dt.datetime, True),
    "created_at": (dt.datetime, False),
    "updated_at": (dt.datetime, False),
}


def test_bank_txn_leg_crossdb_contract_frozen() -> None:
    """bank_txn_leg 列名/类型/可空性必须与冻结契约一致（recon 跨库直读依赖，改动需协同）。"""
    cols = BankTxnLeg.__table__.columns
    actual = {c.name: (c.type.python_type, bool(c.nullable)) for c in cols}

    assert set(actual) == set(_EXPECTED_BANK_TXN_LEG_CONTRACT), (
        "bank_txn_leg 列集合变化（跨库契约漂移）：\n"
        f"  新增: {set(actual) - set(_EXPECTED_BANK_TXN_LEG_CONTRACT)}\n"
        f"  缺失: {set(_EXPECTED_BANK_TXN_LEG_CONTRACT) - set(actual)}\n"
        "改动 bank_txn_leg 列必须同步评估 lending-recon 跨库 collector 并更新本契约快照。"
    )
    for name, (exp_type, exp_nullable) in _EXPECTED_BANK_TXN_LEG_CONTRACT.items():
        act_type, act_nullable = actual[name]
        assert act_type is exp_type, f"列 {name} 类型漂移：期望 {exp_type}, 实际 {act_type}"
        assert act_nullable == exp_nullable, (
            f"列 {name} 可空性漂移：期望 nullable={exp_nullable}, 实际 {act_nullable}"
        )


def test_bank_txn_leg_table_name_frozen() -> None:
    """表名是跨库直读契约的一部分，改名等于断 recon。"""
    assert BankTxnLeg.__tablename__ == "bank_txn_leg"
