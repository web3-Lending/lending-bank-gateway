"""集成测试：在真实 MySQL 8.0 容器中验证两张表的唯一约束。

运行方式（需要 docker）：
    .venv/bin/pytest -m integration --no-cov -q

本机无 docker 时跳过（docker.errors.DockerException → pytest.skip）。
"""

from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def _mysql_sync_engine(url: str) -> sa.Engine:
    return create_engine(url, pool_pre_ping=True)


@pytest.fixture(scope="module")
def mysql_engine():
    """启动 MySQL 8.0 testcontainer，创建表，返回 sync engine。"""
    try:
        from testcontainers.mysql import MySqlContainer  # type: ignore[import-untyped]
    except ImportError:
        pytest.skip("testcontainers 未安装")

    # import 确保模型已注册
    import app.models.txn  # noqa: F401
    from app.models.base import Base

    try:
        with MySqlContainer("mysql:8.0") as mysql:
            # testcontainers 返回无驱动前缀的 mysql://host:port/db；
            # 手动拼接 pymysql URL 避免依赖 MySQLdb
            url = (
                f"mysql+pymysql://{mysql.username}:{mysql.password}"
                f"@{mysql.get_container_host_ip()}:{mysql.get_exposed_port(3306)}"
                f"/{mysql.dbname}"
            )
            engine = _mysql_sync_engine(url)
            Base.metadata.create_all(engine)
            yield engine
            engine.dispose()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"docker/testcontainer 不可用: {exc}")


@pytest.fixture()
def db_session(mysql_engine: sa.Engine):
    """每个测试独立 Session；测试结束后无论成败都关闭（不回滚，每用例独立数据）。"""
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=mysql_engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


# ── helpers ──────────────────────────────────────────────────────────────────

from app.models.txn import BankTxnLeg, BankTxnOrder  # noqa: E402


def _new_order(**kw) -> BankTxnOrder:
    defaults = dict(
        tenant_id="WBTHK01",
        biz_seq_no="DSB-20260611-0001234567890",
        business_action="DISBURSE",
        biz_type="DSB",
        amount=Decimal("100.0000"),
        currency="USD",
        caller_service="lifecycle",
        status="ACCEPTED",
    )
    defaults.update(kw)
    return BankTxnOrder(**defaults)


# ── 测试 ─────────────────────────────────────────────────────────────────────


def test_order_uq_tenant_biz_seq_no_mysql(db_session: Session) -> None:
    """MySQL：同 (tenant_id, biz_seq_no) 应触发 IntegrityError（uq_order_tenant_biz）。"""
    db_session.add(_new_order())
    db_session.flush()
    db_session.add(_new_order())
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_leg_uq_tenant_ext_mysql(db_session: Session) -> None:
    """MySQL uq_leg_tenant_ext: 同 tenant+external_system+external_ref 触发 IntegrityError。"""
    order = _new_order()
    db_session.add(order)
    db_session.flush()

    leg_base = dict(
        tenant_id="WBTHK01",
        order_id=order.id,
        biz_seq_no=order.biz_seq_no,
        external_system="WEDAP_BANK",
        step_type="DISBURSEMENT_COLLECTION",
        external_ref="HSBC202606110001",
        amount=Decimal("100.0000"),
        currency="USD",
        status="SUCCESS",
    )
    db_session.add(BankTxnLeg(**{**leg_base, "step_seq": 1}))
    db_session.flush()
    # 同 external_ref 不同 step_seq → 仍撞 uq_leg_tenant_ext
    db_session.add(BankTxnLeg(**{**leg_base, "step_seq": 2}))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_leg_uq_tenant_biz_step_mysql(db_session: Session) -> None:
    """MySQL：同 (tenant_id, biz_seq_no, step_seq) 触发 IntegrityError (uq_leg_tenant_biz_step)。"""
    order = _new_order()
    db_session.add(order)
    db_session.flush()

    leg_base = dict(
        tenant_id="WBTHK01",
        order_id=order.id,
        biz_seq_no=order.biz_seq_no,
        external_system="WEDAP_BANK",
        step_type="DISBURSEMENT_COLLECTION",
        step_seq=1,
        amount=Decimal("100.0000"),
        currency="USD",
        status="SUCCESS",
    )
    db_session.add(BankTxnLeg(**{**leg_base, "external_ref": "HSBC202606110001"}))
    db_session.flush()
    # 不同 external_ref 同 step_seq=1 → 撞 uq_leg_tenant_biz_step
    db_session.add(BankTxnLeg(**{**leg_base, "external_ref": "HSBC202606110002"}))
    with pytest.raises(IntegrityError):
        db_session.flush()
