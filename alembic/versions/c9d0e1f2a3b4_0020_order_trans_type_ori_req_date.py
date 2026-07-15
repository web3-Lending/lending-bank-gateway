"""0020_order_trans_type_ori_req_date

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-14 20:00:00.000000

bank_txn_order 增加 trans_type / ori_req_date 两列，为状态回查切 wedap 通用
`GET /api/v1/transactions/status` 供参（该接口四必填：bizSeqNo/transType/oriReqDate/
oriBizSeqNo，2026-07-14 DEV 实测锚定）：

- trans_type：提交时调用方发的 transType 原值。wedap 按 (oriBizSeqNo, transType) 消歧
  同号多行（分发复用归集号场景），查询值必须**等于提交值**，故存原值而非从 biz_type 反推。
- ori_req_date：提交日 YYYYMMDD（bank_timezone 换算），避免 UTC 跨午夜查不到原单。

存量行 NULL：reconcile / status 视图对 NULL 跳过 wedap 回查（交 G6 告警人工处置），
不做 biz_type 反推兜底——反推错了就是又一轮假终态（W7 教训）。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bank_txn_order", sa.Column("trans_type", sa.String(length=20), nullable=True))
    op.add_column("bank_txn_order", sa.Column("ori_req_date", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("bank_txn_order", "ori_req_date")
    op.drop_column("bank_txn_order", "trans_type")
