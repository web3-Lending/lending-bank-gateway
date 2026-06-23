"""跨库直读契约（gateway 侧）—— A-M-004 + SSOT（G3）。

ADR-0031/README：lending-recon 通过跨库 collector **直读** gateway 库的 `bank_txn_leg` 物理表
（Plan 3），绕过 HTTP 供数路径。该物理表因此成为跨服务契约，但物理表本身无版本化保护——
gateway 改列名/类型/长度/精度/可空性/约束会让 recon 侧静默错账且无 CI 信号。

本测试把 `bank_txn_leg` 的 **DB 级**形态冻结为 checked-in 契约 SSOT
（contracts/bank-txn-leg-contract.json）：
  - 每列：python 类型 + 长度 + precision/scale + 可空性
  - 主键列集合 / 唯一约束（名 → **列集合**）/ 外键（名 + 列 + 引用）
  - schema_version
任何漂移 → red，强制改动者同步评估 recon 跨库 collector、协同更新（recon 侧契约测试 + 迁移）后
再更新此快照。recon 仓侧契约测试为独立 followup（FU-GW-RECON-CROSSDB-CONTRACT，需跨仓协作）。

SSOT 文档：lending-workspace/05-reference/contracts/bank-txn-leg-contract.md。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, cast

from sqlalchemy import ForeignKeyConstraint, Table, UniqueConstraint

from app.models.txn import BankTxnLeg

_CONTRACT = pathlib.Path(__file__).parent.parent.parent / "contracts" / "bank-txn-leg-contract.json"


def _column_spec(column: Any) -> dict[str, Any]:
    """从模型列抽取 DB 级契约 spec：python 类型 + 长度 + precision/scale + 可空性。"""
    col_type = column.type
    spec: dict[str, Any] = {
        "python_type": col_type.python_type.__name__,
        "nullable": bool(column.nullable),
    }
    if getattr(col_type, "length", None) is not None:
        spec["length"] = col_type.length
    if getattr(col_type, "precision", None) is not None:
        spec["precision"] = col_type.precision
        spec["scale"] = col_type.scale
    return spec


def _live_contract() -> dict[str, Any]:
    table = cast(Table, BankTxnLeg.__table__)
    cols = {c.name: _column_spec(c) for c in table.columns}
    uqs_unsorted = {
        str(con.name): [c.name for c in con.columns]
        for con in table.constraints
        if isinstance(con, UniqueConstraint) and con.name is not None
    }
    uqs = {name: uqs_unsorted[name] for name in sorted(uqs_unsorted)}
    fks = sorted(
        (
            {
                "name": con.name,
                "columns": list(con.column_keys),
                "references": [e.target_fullname for e in con.elements],
            }
            for con in table.constraints
            if isinstance(con, ForeignKeyConstraint)
        ),
        key=lambda fk: str(fk["name"]),
    )
    return {
        "schema_version": "1",
        "table": BankTxnLeg.__tablename__,
        "columns": cols,
        "primary_key": [c.name for c in table.primary_key.columns],
        "unique_constraints": uqs,
        "foreign_keys": fks,
    }


def test_bank_txn_leg_crossdb_contract_frozen() -> None:
    """bank_txn_leg DB 级形态（列 + 主键 + 唯一约束列集合 + 外键）必须与 checked-in SSOT 一致。

    漂移（含同名唯一约束改列集合）→ 改动者必须同步评估 lending-recon 跨库 collector、
    更新 recon 侧契约测试，再更新 contracts/bank-txn-leg-contract.json（评审可见）。
    """
    assert _CONTRACT.exists(), "缺 contracts/bank-txn-leg-contract.json 契约 SSOT 基准"
    frozen = json.loads(_CONTRACT.read_text())
    live = _live_contract()
    assert live == frozen, (
        "bank_txn_leg 跨库契约漂移（列/类型/长度/精度/可空/主键/唯一约束列集合/外键/版本）：\n"
        f"  live   = {live}\n"
        f"  frozen = {frozen}\n"
        "改动必须同步评估 recon 跨库 collector 并更新 contracts/bank-txn-leg-contract.json。"
    )


def test_bank_txn_leg_table_name_frozen() -> None:
    """表名是跨库直读契约的一部分，改名等于断 recon。"""
    assert BankTxnLeg.__tablename__ == "bank_txn_leg"
