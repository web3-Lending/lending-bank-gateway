"""集成测试：在真实 MySQL 8.0 容器中验证两张表的唯一约束、复合 FK 跨租户保护、
以及 audit_log append-only 触发器。

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
            # root URL（用于 GLOBAL 变量设置，需要 SUPER 或 SYSTEM_VARIABLES_ADMIN 权限）
            root_url = (
                f"mysql+pymysql://root:test"
                f"@{mysql.get_container_host_ip()}:{mysql.get_exposed_port(3306)}"
                f"/{mysql.dbname}"
            )
            # binary logging 开启时创建 MySQL trigger 需要 log_bin_trust_function_creators=ON；
            # testcontainer 的普通用户无 SYSTEM_VARIABLES_ADMIN，用 root 预先设置。
            root_engine = _mysql_sync_engine(root_url)
            with root_engine.connect() as conn:
                conn.execute(sa.text("SET GLOBAL log_bin_trust_function_creators = 1"))
                conn.commit()
            root_engine.dispose()

            engine = _mysql_sync_engine(url)
            Base.metadata.create_all(engine)
            yield engine
            engine.dispose()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"docker/testcontainer 不可用: {exc}")


@pytest.fixture()
def db_session(mysql_engine: sa.Engine):
    """每个测试独立 Session；测试结束后先 rollback 再 close，防测试间数据污染。"""
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=mysql_engine, expire_on_commit=False)
    session = factory()
    yield session
    session.rollback()
    session.close()


# ── helpers ──────────────────────────────────────────────────────────────────

from app.models.callback import CallbackInbox  # noqa: E402
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


def test_leg_cross_tenant_order_ref_rejected_mysql(db_session: Session) -> None:
    """MySQL：leg.tenant_id != order.tenant_id 时复合 FK fk_leg_order_tenant 拒绝插入。

    场景：order 属于 TENANT-A，leg 用 TENANT-B 引用该 order → IntegrityError。
    复合 FK: leg.(order_id, tenant_id) → order.(id, tenant_id)。
    """
    order_a = _new_order(tenant_id="TENANT-A", biz_seq_no="DSB-A-CROSS-001")
    db_session.add(order_a)
    db_session.flush()

    cross_leg = BankTxnLeg(
        tenant_id="TENANT-B",  # 与 order_a.tenant_id 不同
        order_id=order_a.id,
        biz_seq_no="DSB-B-CROSS-001",
        external_system="WEDAP_BANK",
        external_ref="HSBC-CROSS-MYSQL-0001",
        step_type="DISBURSEMENT_COLLECTION",
        step_seq=1,
        amount=Decimal("50.0000"),
        currency="USD",
        status="PENDING",
    )
    db_session.add(cross_leg)
    with pytest.raises(IntegrityError):
        db_session.flush()


# ── UTC 钉验证 ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def mysql_async_url(mysql_engine: sa.Engine) -> str:
    """从 sync engine URL 派生 asyncmy URL，使用 root 凭证。

    testcontainers MySQL 8.0 默认使用 caching_sha2_password，asyncmy 在特定网络拓扑
    （WSL2 docker bridge）下以 test 用户连接会报 Access denied。
    root 用户有 '%' 授权且认证兼容，用于验证 _pin_utc 钩子是否生效。
    """
    try:
        import importlib.util

        if importlib.util.find_spec("testcontainers") is None:
            pytest.skip("testcontainers 未安装")
    except Exception:
        pytest.skip("testcontainers 不可用")

    # 从 sync URL 提取 host:port/db，用 root 凭证重新拼 asyncmy URL
    sync_url = str(mysql_engine.url)
    # sync_url = mysql+pymysql://test:test@localhost:PORT/test
    # 提取 @host:port/db 部分
    at_pos = sync_url.index("@")
    host_db = sync_url[at_pos + 1 :]  # host:port/db
    # testcontainers 默认 root_password="test"；此处非生产凭证，仅测试容器用
    tc_root_pw = "test"  # noqa: S105
    return f"mysql+asyncmy://root:{tc_root_pw}@{host_db}"


@pytest.mark.asyncio
async def test_utc_pin_async(mysql_async_url: str) -> None:
    """验证 _pin_utc 钩子在 async engine 连接后把 session time_zone 设为 +00:00。

    使用 mysql+asyncmy:// URL 构建 async engine，执行 SELECT @@session.time_zone，
    断言结果等于 '+00:00'。
    """
    try:
        import asyncmy  # noqa: F401
    except ImportError:
        pytest.skip("asyncmy 未安装")

    from app.core.db import build_engine

    async_engine = build_engine(mysql_async_url)
    try:
        from sqlalchemy import text as sa_text
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        async with factory() as session:
            result = await session.execute(sa_text("SELECT @@session.time_zone"))
            tz_value = result.scalar_one()
        assert tz_value == "+00:00", f"expected '+00:00', got {tz_value!r}"
    finally:
        await async_engine.dispose()


# ── 迁移执行 + ON UPDATE CURRENT_TIMESTAMP 验证 ──────────────────────────────


def test_alembic_upgrade_head_and_on_update_mysql(mysql_engine: sa.Engine) -> None:
    """验证 0002 迁移可在 MySQL 上执行，且 bank_txn_order.updated_at 有 ON UPDATE 行为。

    流程：
    1. drop_all 后用 alembic command.upgrade("head") 跑完整迁移链（进程内，复用
       mysql_engine 连接，不 subprocess —— 规避 testcontainer test 用户认证限制）。
    2. 插入一条 bank_txn_order，记录 updated_at 初始值。
    3. sleep(1) 后 raw SQL UPDATE status；重查 updated_at 验证自动更新。
    """
    import time
    from decimal import Decimal

    from alembic.config import Config

    from alembic import command
    from app.models.base import Base

    # 1. drop_all → alembic upgrade head（进程内，复用 mysql_engine 连接）
    Base.metadata.drop_all(mysql_engine)

    worktree = "/home/ubuntu/lending/lending-bank-gateway/.worktrees/v1-cutover-core"
    alembic_cfg = Config(f"{worktree}/alembic.ini")
    alembic_cfg.set_main_option("script_location", f"{worktree}/alembic")

    # 直接传入同步连接，跳过 env.py 的 URL 读取路径
    with mysql_engine.connect() as conn:
        alembic_cfg.attributes["connection"] = conn
        command.upgrade(alembic_cfg, "head")

    # 2. 插入一条记录，捕获初始 updated_at
    with mysql_engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO bank_txn_order "
                "(tenant_id, biz_seq_no, business_action, biz_type, amount, currency,"
                " caller_service, status) "
                "VALUES ('T1','SEQ-ON-UPDATE-001','DISBURSE','DSB',:amt,'USD','svc','ACCEPTED')"
            ),
            {"amt": Decimal("100.0000")},
        )
        conn.commit()

        before = conn.execute(
            sa.text("SELECT updated_at FROM bank_txn_order WHERE biz_seq_no='SEQ-ON-UPDATE-001'")
        ).scalar_one()

        # 等 1 秒确保时间戳可区分（MySQL DATETIME 精度为秒）
        time.sleep(1)

        conn.execute(
            sa.text(
                "UPDATE bank_txn_order SET status='SUBMITTED' WHERE biz_seq_no='SEQ-ON-UPDATE-001'"
            )
        )
        conn.commit()

        after = conn.execute(
            sa.text("SELECT updated_at FROM bank_txn_order WHERE biz_seq_no='SEQ-ON-UPDATE-001'")
        ).scalar_one()

    assert after > before, f"ON UPDATE CURRENT_TIMESTAMP 未生效：before={before!r}, after={after!r}"


def test_inbox_uq_tenant_src_req_mysql(db_session: Session) -> None:
    """MySQL：同 (tenant_id, source, request_id) 触发 IntegrityError (uq_inbox_tenant_src_req)。"""
    row = dict(
        tenant_id="WBTHK01", source="WEDAP_TXN", request_id="CB-20260611-001", payload={"k": 1}
    )
    db_session.add(CallbackInbox(**row))
    db_session.flush()
    db_session.add(CallbackInbox(**row))
    with pytest.raises(IntegrityError):
        db_session.flush()


# ── audit_log append-only 触发器验证 ─────────────────────────────────────────


def test_audit_log_update_rejected_by_trigger_mysql(mysql_engine: sa.Engine) -> None:
    """MySQL：UPDATE audit_log 应被 BEFORE UPDATE 触发器拒绝（SIGNAL SQLSTATE '45000'）。

    alembic 0005 创建触发器，本测试在 upgrade head 之后的 mysql_engine 上直接执行 raw SQL
    验证触发器有效性。依赖 test_alembic_upgrade_head_and_on_update_mysql 已执行 upgrade head。
    """
    from sqlalchemy.exc import OperationalError

    with mysql_engine.connect() as conn:
        # 先插入一条合法的 audit_log
        conn.execute(
            sa.text(
                "INSERT INTO audit_log (tenant_id, actor, action, entity, prev_hash, row_hash) "
                "VALUES ('T-AUDIT', 'system', 'TEST', 'entity:1', :ph, :rh)"
            ),
            {"ph": "0" * 64, "rh": "1" * 64},
        )
        conn.commit()

        # UPDATE 应被触发器拒绝
        with pytest.raises((IntegrityError, OperationalError)):
            conn.execute(sa.text("UPDATE audit_log SET actor='tamper' WHERE tenant_id='T-AUDIT'"))
            conn.commit()
