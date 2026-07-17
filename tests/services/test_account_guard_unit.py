"""account_guard / platform_accounts 直调单元测试。

TestClient 的 portal 线程在 py3.12 coverage 下对部分协程帧记录不稳
（本仓 worker 一贯用「核心逻辑主线程直调」范式拿覆盖，见 workers/* docstring）；
端点级行为已由 tests/api/ 两文件断言，本文件以直调补齐同逻辑的行覆盖。
"""

import asyncio

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.main import create_app
from app.models.base import Base
from app.models.platform_account import PlatformBankAccount
from app.services.account_guard import (
    REASON_CURRENCY,
    REASON_DISABLED,
    REASON_NOT_REGISTERED,
    REASON_SCOPE,
    _evaluate,
    assert_platform_account_allowed,
)

TENANT = "OCBC"
ESCROW = "ESCROW-USD-001"


def _mk_app(**settings_overrides):  # type: ignore[no-untyped-def]
    app = create_app(settings=Settings(**settings_overrides))

    async def _tables() -> None:
        async with app.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_tables())
    return app


async def _seed(factory, **overrides):  # type: ignore[no-untyped-def]
    row = dict(
        tenant_id=TENANT,
        account_no=ESCROW,
        purpose="escrow",
        allowed_scopes="bank_collect,bank_distribute",
        currency=None,
        status="active",
    )
    row.update(overrides)
    async with factory() as session:
        async with session.begin():
            session.add(PlatformBankAccount(**row))


def test_evaluate_matrix_direct() -> None:
    """_evaluate 五分支矩阵：未登记/禁用/scope/币种/放行。"""
    app = _mk_app()
    factory = app.state.session_factory

    async def _run() -> None:
        await _seed(factory)
        await _seed(factory, account_no="ESCROW-HKD-002", currency="HKD")
        await _seed(factory, account_no="ESCROW-DIS-003", status="disabled")
        async with factory() as session:
            base = {"tenant_id": TENANT, "business_scope": "bank_collect", "currency": "USD"}
            v = await _evaluate(session, account_no="NOPE", **base)
            assert (v.ok, v.reason) == (False, REASON_NOT_REGISTERED)
            v = await _evaluate(session, account_no="ESCROW-DIS-003", **base)
            assert (v.ok, v.reason) == (False, REASON_DISABLED)
            v = await _evaluate(
                session,
                account_no=ESCROW,
                tenant_id=TENANT,
                business_scope="bank_refund",
                currency="USD",
            )
            assert (v.ok, v.reason) == (False, REASON_SCOPE)
            v = await _evaluate(session, account_no="ESCROW-HKD-002", **base)
            assert (v.ok, v.reason) == (False, REASON_CURRENCY)
            v = await _evaluate(session, account_no=ESCROW, **base)
            assert (v.ok, v.reason) == (True, None)

    asyncio.run(_run())


def test_guard_entry_direct_ok_observe_enforce() -> None:
    """入口三态直调：放行 return / observe 不抛 / enforce 抛 403。"""
    app = _mk_app()
    factory = app.state.session_factory
    common = {
        "tenant_id": TENANT,
        "business_scope": "bank_collect",
        "currency": "USD",
        "caller": "lifecycle",
        "trace_id": "trc-unit",
    }

    async def _run() -> None:
        await _seed(factory)
        # ok 路径（verdict.ok → 提前 return）
        await assert_platform_account_allowed(factory, ESCROW, mode="enforce", **common)
        # observe：非法但不抛
        await assert_platform_account_allowed(factory, "NOPE", mode="observe", **common)
        # enforce：非法 403
        with pytest.raises(HTTPException) as exc:
            await assert_platform_account_allowed(factory, "NOPE", mode="enforce", **common)
        assert exc.value.status_code == 403
        assert exc.value.detail["reason"] == REASON_NOT_REGISTERED

    asyncio.run(_run())


# ── platform_accounts 端点直调（stub Request） ────────────────────────────────


class _StubRequest:
    def __init__(self, app, caller: str = "fund-ops") -> None:  # type: ignore[no-untyped-def]
        self.app = app
        self.headers = {"X-Caller-Service": caller}


def test_admin_endpoints_direct_roundtrip() -> None:
    from app.api.v1.platform_accounts import (
        PlatformAccountCreate,
        PlatformAccountPatch,
        create_platform_account,
        list_platform_accounts,
        patch_platform_account,
    )

    app = _mk_app(admin_callers="fund-ops")
    req = _StubRequest(app)

    async def _run() -> None:
        created = await create_platform_account(
            PlatformAccountCreate(
                tenantId=TENANT,
                accountNo=ESCROW,
                purpose="escrow",
                allowedScopes="bank_collect",
            ),
            req,  # type: ignore[arg-type]
        )
        row_id = created["data"]["id"]
        listed = await list_platform_accounts(req, tenantId=TENANT)  # type: ignore[arg-type]
        assert listed["data"]["count"] == 1
        patched = await patch_platform_account(
            row_id,
            PlatformAccountPatch(
                purpose="settlement",
                allowedScopes="bank_distribute",
                currency="USD",
                status="disabled",
                note="unit",
            ),
            req,  # type: ignore[arg-type]
        )
        assert patched["data"]["status"] == "disabled"
        assert patched["data"]["purpose"] == "settlement"
        # no-change PATCH：不写审计、返回现状
        unchanged = await patch_platform_account(
            row_id,
            PlatformAccountPatch(),
            req,  # type: ignore[arg-type]
        )
        assert unchanged["data"]["status"] == "disabled"
        # 404
        with pytest.raises(HTTPException) as exc:
            await patch_platform_account(9999, PlatformAccountPatch(status="active"), req)  # type: ignore[arg-type]
        assert exc.value.status_code == 404

    asyncio.run(_run())


def test_guard_entry_direct_edge_branches() -> None:
    """off 短路 / 缺账号 REASON_MISSING 直调补线。"""
    from app.services.account_guard import REASON_MISSING

    app = _mk_app()
    factory = app.state.session_factory
    common = {
        "tenant_id": TENANT,
        "business_scope": "bank_collect",
        "currency": "USD",
        "caller": "lifecycle",
        "trace_id": "trc-unit",
    }

    async def _run() -> None:
        # off：不查库直接返回
        await assert_platform_account_allowed(factory, "ANY", mode="off", **common)
        # 缺账号（None）→ REASON_MISSING
        with pytest.raises(HTTPException) as exc:
            await assert_platform_account_allowed(factory, None, mode="enforce", **common)
        assert exc.value.detail["reason"] == REASON_MISSING

    asyncio.run(_run())


def test_admin_endpoints_direct_error_branches() -> None:
    from sqlalchemy.exc import IntegrityError  # noqa: F401  (409 路径由重复 create 触发)

    from app.api.v1.platform_accounts import (
        PlatformAccountCreate,
        PlatformAccountPatch,
        create_platform_account,
        patch_platform_account,
    )

    app = _mk_app(admin_callers="fund-ops")
    req = _StubRequest(app)
    body = PlatformAccountCreate(
        tenantId=TENANT, accountNo=ESCROW, purpose="escrow", allowedScopes="bank_collect"
    )

    empty_app = _mk_app(admin_callers="")

    async def _run() -> None:
        # caller 门禁：空白名单 fail-closed + caller 不在册
        with pytest.raises(HTTPException) as exc:
            await create_platform_account(body, _StubRequest(empty_app))  # type: ignore[arg-type]
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            await create_platform_account(body, _StubRequest(app, caller="lifecycle"))  # type: ignore[arg-type]
        assert exc.value.status_code == 403
        # 非法 status（create/patch）
        bad = body.model_copy(update={"status": "frozen"})
        with pytest.raises(HTTPException) as exc:
            await create_platform_account(bad, req)  # type: ignore[arg-type]
        assert exc.value.status_code == 400
        with pytest.raises(HTTPException) as exc:
            await patch_platform_account(1, PlatformAccountPatch(status="frozen"), req)  # type: ignore[arg-type]
        assert exc.value.status_code == 400
        # 非法 scopes csv
        with pytest.raises(HTTPException) as exc:
            await create_platform_account(
                body.model_copy(update={"allowedScopes": "BAD SCOPE"}),
                req,  # type: ignore[arg-type]
            )
        assert exc.value.status_code == 400
        # 重复登记 409
        await create_platform_account(body, req)  # type: ignore[arg-type]
        with pytest.raises(HTTPException) as exc:
            await create_platform_account(body, req)  # type: ignore[arg-type]
        assert exc.value.status_code == 409

    asyncio.run(_run())


class _BoomFactory:
    """session_factory 替身：进门即炸，模拟守卫基础设施故障。"""

    def __call__(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")


def test_observe_infra_failure_allows_enforce_fails() -> None:
    """codex P1：observe=纯观察，守卫自身故障不阻断业务；enforce 保持 fail-closed。"""
    common = {
        "tenant_id": TENANT,
        "business_scope": "bank_collect",
        "currency": "USD",
        "caller": "lifecycle",
        "trace_id": "trc-unit",
    }

    async def _run() -> None:
        # observe：infra 炸 → 放行（不抛）
        await assert_platform_account_allowed(_BoomFactory(), ESCROW, mode="observe", **common)  # type: ignore[arg-type]
        # enforce：infra 炸 → fail-closed 上抛
        with pytest.raises(RuntimeError, match="db down"):
            await assert_platform_account_allowed(_BoomFactory(), ESCROW, mode="enforce", **common)  # type: ignore[arg-type]

    asyncio.run(_run())


def test_patch_explicit_null_clears_currency_note() -> None:
    """codex P2：显式 null 清空 currency/note；必填列显式 null → 400。"""
    from app.api.v1.platform_accounts import (
        PlatformAccountCreate,
        PlatformAccountPatch,
        create_platform_account,
        patch_platform_account,
    )

    app = _mk_app(admin_callers="fund-ops")
    req = _StubRequest(app)

    async def _run() -> None:
        created = await create_platform_account(
            PlatformAccountCreate(
                tenantId=TENANT,
                accountNo=ESCROW,
                purpose="escrow",
                allowedScopes="bank_collect",
                currency="USD",
                note="x",
            ),
            req,  # type: ignore[arg-type]
        )
        row_id = created["data"]["id"]
        # 显式 null 清空（模拟 JSON {"currency": null, "note": null}）
        patched = await patch_platform_account(
            row_id,
            PlatformAccountPatch.model_validate({"currency": None, "note": None}),
            req,  # type: ignore[arg-type]
        )
        assert patched["data"]["currency"] is None
        assert patched["data"]["note"] is None
        # 字段缺省 = 不动
        untouched = await patch_platform_account(
            row_id,
            PlatformAccountPatch(status="disabled"),
            req,  # type: ignore[arg-type]
        )
        assert untouched["data"]["currency"] is None
        # 必填列显式 null → 400
        for f in ("purpose", "allowedScopes", "status"):
            with pytest.raises(HTTPException) as exc:
                await patch_platform_account(
                    row_id,
                    PlatformAccountPatch.model_validate({f: None}),
                    req,  # type: ignore[arg-type]
                )
            assert exc.value.status_code == 400

    asyncio.run(_run())


def test_admin_caller_token_binding_fail_fast() -> None:
    """codex P0：非 local/test 下 admin caller 必须有 per-service token 绑定。"""
    base = {
        "env": "dev",
        "s2s_secret": "shared-secret",
        "wedap_callback_api_key": "cb-key",
        "admin_callers": "fund-ops",
        "allow_sqlite_db": True,
    }
    with pytest.raises(RuntimeError, match="GW_ADMIN_CALLERS"):
        create_app(settings=Settings(**base))
    # 绑定齐全 → 正常启动
    create_app(settings=Settings(**base, s2s_caller_tokens="fund-ops:tok-1"))
    # local 豁免
    create_app(settings=Settings(admin_callers="fund-ops"))


def test_admin_caller_empty_token_not_bound_fail_fast() -> None:
    """codex R2 P0：`fund-ops:` 空 token 必须视为未绑定（与运行时同一解析）。"""
    base = {
        "env": "dev",
        "s2s_secret": "shared-secret",
        "wedap_callback_api_key": "cb-key",
        "admin_callers": "fund-ops",
        "allow_sqlite_db": True,
    }
    for bad in ("fund-ops:", "fund-ops:   ", ":tok", "fund-ops"):
        with pytest.raises(RuntimeError, match="GW_ADMIN_CALLERS"):
            create_app(settings=Settings(**base, s2s_caller_tokens=bad))


def test_parse_caller_tokens_single_authority() -> None:
    from app.core.s2s import parse_caller_tokens

    assert parse_caller_tokens("") is None
    assert parse_caller_tokens("fund-ops:") is None
    assert parse_caller_tokens("a:1, b:2 ,c:,:d") == {"a": "1", "b": "2"}


def test_hybrid_s2s_existing_callers_unaffected_and_admin_needs_token() -> None:
    """codex R2 P1：给 admin 配专属 token 不打断存量共享 secret caller；
    共享 secret 伪造 admin caller 头到不了 admin 端点（token_bound 拦截）。"""
    from fastapi.testclient import TestClient

    app = create_app(
        settings=Settings(
            env="dev",
            s2s_secret="shared-secret",  # noqa: S106 (测试固定值)
            wedap_callback_api_key="cb-key",
            admin_callers="fund-ops",
            s2s_caller_tokens="fund-ops:tok-1",
            allow_sqlite_db=True,  # 模拟 dev 分支 + 内存库（过 db_url fail-fast 守卫）
        )
    )

    async def _tables() -> None:
        async with app.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_tables())
    c = TestClient(app)

    # 存量 caller（不在 token 表）：共享 secret 照常通过 S2S（打到健康业务读接口）
    r = c.get(
        "/api/v1/admin/stuck-orders",
        headers={
            "X-Caller-Service": "lifecycle",
            "X-S2S-Token": "shared-secret",
            "X-Tenant-Id": TENANT,
            "X-Request-Id": "r-1",
        },
    )
    assert r.status_code == 200

    admin_headers = {
        "X-Caller-Service": "fund-ops",
        "X-Tenant-Id": TENANT,
        "X-Request-Id": "r-2",
    }
    # 伪造：共享 secret + admin caller 头 → S2S 层要求 fund-ops 专属 token → 401
    r = c.get(
        f"/api/v1/admin/platform-accounts?tenantId={TENANT}",
        headers=admin_headers | {"X-S2S-Token": "shared-secret"},
    )
    assert r.status_code == 401
    # 正身：专属 token → token_bound → 放行
    r = c.get(
        f"/api/v1/admin/platform-accounts?tenantId={TENANT}",
        headers=admin_headers | {"X-S2S-Token": "tok-1"},
    )
    assert r.status_code == 200


def test_admin_token_bound_defense_in_depth_direct() -> None:
    """纵深防御分支直调：非 local/test 且请求无 token_bound 标记（如中间件被绕过）→ 403。"""
    from app.api.v1.platform_accounts import list_platform_accounts

    app = create_app(
        settings=Settings(
            env="dev",
            s2s_secret="shared-secret",  # noqa: S106 (测试固定值)
            wedap_callback_api_key="cb-key",
            admin_callers="fund-ops",
            s2s_caller_tokens="fund-ops:tok-1",
            allow_sqlite_db=True,  # 模拟 dev 分支 + 内存库（过 db_url fail-fast 守卫）
        )
    )

    class _NoBoundState:
        pass

    req = _StubRequest(app)
    req.state = _NoBoundState()  # type: ignore[attr-defined]

    async def _run() -> None:
        with pytest.raises(HTTPException) as exc:
            await list_platform_accounts(req, tenantId=TENANT)  # type: ignore[arg-type]
        assert exc.value.status_code == 403
        assert "per-service token" in exc.value.detail["message"]

    asyncio.run(_run())


def test_credential_collision_fail_fast() -> None:
    """codex R3 P1：token==共享 secret 或 caller 间 token 重复 → 启动期拦截。"""
    base = {"env": "dev", "wedap_callback_api_key": "cb-key", "allow_sqlite_db": True}
    # token 与共享 secret 相同
    with pytest.raises(RuntimeError, match="GW_S2S_SECRET 相同"):
        create_app(
            settings=Settings(**base, s2s_secret="X", s2s_caller_tokens="fund-ops:X")  # noqa: S106
        )
    # 两 caller 共用 token
    with pytest.raises(RuntimeError, match="token 值重复"):
        create_app(
            settings=Settings(
                **base,
                s2s_secret="S",  # noqa: S106 (测试固定值)
                s2s_caller_tokens="a:T1,b:T1",
            )
        )
    # 互异 → 正常
    create_app(
        settings=Settings(**base, s2s_secret="S", s2s_caller_tokens="a:T1,b:T2")  # noqa: S106
    )
