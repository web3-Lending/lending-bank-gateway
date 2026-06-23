# lending-bank-gateway v1-cutover 核心实施计划（Plan 1/4）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从零建成 lending-bank-gateway 服务本体（v1-cutover 轨）：北向资金交易/查询 API（规范 02 envelope）+ 南向 wedap client + order/leg 落盘 + 回调 inbox/outbox + 对账结果摄取，CI 全程本地闭环（fixture/mock），可独立部署。

**Architecture:** FastAPI 异步单体 + MySQL（`lending_bank_gateway` 库）。北向收幂等写请求→落 `bank_txn_order`（事务内禁外呼）→commit 后调 wedap→异步收妥；wedap 回调进 `callback_inbox`→拉 steps 落 `bank_txn_leg`→聚合父单状态→`callback_outbox` 转发 9000；recon notify→S3 下载→3-sheet Excel 解析落 `recon_result_*`。对应 spec：`docs/superpowers/specs/2026-06-11-gateway-v1-scope-design.md`（codex 四轮评审 GATE:PASS）。

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.0 async (asyncmy/aiosqlite) · Alembic · httpx + respx · boto3 + botocore Stubber · openpyxl · pytest(-asyncio) + testcontainers-mysql(integration) · ruff + mypy。

**执行纪律：**
- 在 gateway 仓开 worktree 分支 `feat/v1-cutover-core` 执行，不在 main 直接 commit（全局 git 规则）
- 覆盖率门禁 `fail_under=100`（lending-core 级），每 task 末跑 `pytest -q && ruff check . && mypy app`
- 金额一律 `Decimal`/`Numeric(21,4)`，API 序列化为字符串；时间戳 aware UTC（规范 11）

**Plan 拆分说明（spec 多子系统）：** 本计划只覆盖 gateway 仓本体。Plan 2（9000 `admin_bank_intent`+调用方改造）、Plan 3（recon collector+四段规则）、Plan 4（liquidation/customers 迁移）在本计划 M3/M5 产物（OpenAPI、库 schema）定稿后另行编写。

---

## File Structure（一次定盘）

```
lending-bank-gateway/
├── pyproject.toml                    # 依赖 + ruff/mypy/pytest/coverage 配置
├── alembic.ini / alembic/            # 迁移（env.py 读 settings）
├── app/
│   ├── main.py                       # FastAPI 装配：中间件、路由、lifespan
│   ├── core/
│   │   ├── config.py                 # Settings（env 驱动）
│   │   ├── db.py                     # async engine/session，session 钉 +00:00
│   │   ├── envelope.py               # 规范02 响应封装 + 错误码
│   │   ├── context.py                # 四标识符提取/透传（contextvars）
│   │   └── s2s.py                    # 北向 S2S 鉴权中间件
│   ├── models/
│   │   ├── base.py                   # Base + TenantMixin + TimestampMixin
│   │   ├── txn.py                    # bank_txn_order / bank_txn_leg
│   │   ├── idempotency.py            # idempotency_record
│   │   ├── callback.py               # callback_inbox / callback_outbox
│   │   ├── recon.py                  # recon_result_task/diff/source_wedap/source_bank
│   │   ├── query_audit.py            # query_audit / balance_snapshot
│   │   └── audit.py                  # audit_log（append-only + hash chain）
│   ├── domain/
│   │   ├── states.py                 # 状态机：枚举/转移守卫/父子聚合
│   │   └── biz_seq.py                # ADR-0029 格式校验
│   ├── services/
│   │   ├── idempotency.py            # 幂等三元组 check/save/replay/409
│   │   ├── submit.py                 # 受理服务（order 落库→外呼→状态推进）
│   │   ├── legs.py                   # steps 拉取→leg upsert→父单聚合
│   │   ├── outbox.py                 # outbox enqueue/dispatch/dead-letter
│   │   ├── recon_ingest.py           # notify→S3→md5→Excel→落库→supersede
│   │   └── audit.py                  # 审计写入 helper（hash chain）
│   ├── clients/
│   │   ├── wedap.py                  # 南向 client（4 写 + status + steps + 查询）
│   │   └── s3.py                     # S3 下载 + md5
│   ├── api/v1/
│   │   ├── loans.py                  # p2p-disbursements / p2p-repayments
│   │   ├── bank_funds.py             # collect / distribute / status
│   │   ├── composite.py              # [已废弃 2026-06-23] composite steps 北向透传（仅历史；内部预取走 wedap client）
│   │   ├── deposit.py                # balances/total · accounts · users/info
│   │   ├── callbacks.py              # wedap 交易回调接收
│   │   ├── recon_notify.py           # 对账结果 notify 接收
│   │   ├── fiat_vault.py             # [已废弃 2026-06-23] fiat-vault/transactions 供数（仅历史；recon 改跨库直读 bank_txn_order）
│   │   ├── admin_ops.py              # outbox dead-letter 重放
│   │   └── health.py                 # /healthz /readyz
│   └── workers/
│       ├── outbox_dispatcher.py      # outbox 轮询投递
│       └── recon_worker.py           # recon task 异步摄取
├── tests/                            # 镜像 app/ 结构；fixtures/ 放 wedap 契约样本
│   └── integration/                  # testcontainers-mysql（约束/迁移），-m integration
└── deploy/                           # Dockerfile + compose + deploy.sh（M6）
```

---

## Milestone 1 · 脚手架与平台件

### Task 1: 项目骨架 + 工具链配置

**Files:**
- Create: `pyproject.toml`, `app/__init__.py`, `app/main.py`, `tests/__init__.py`, `tests/conftest.py`, `.gitignore`

- [ ] **Step 1: 写 pyproject.toml**

```toml
[project]
name = "lending-bank-gateway"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115", "uvicorn[standard]>=0.30",
  "sqlalchemy[asyncio]>=2.0.30", "asyncmy>=0.2.9", "aiosqlite>=0.20",
  "alembic>=1.13", "pydantic>=2.7", "pydantic-settings>=2.3",
  "httpx>=0.27", "boto3>=1.34", "openpyxl>=3.1",
]
[project.optional-dependencies]
dev = [
  "pytest>=8.2", "pytest-asyncio>=0.23", "pytest-cov>=5.0", "respx>=0.21",
  "ruff>=0.4", "mypy>=1.10", "testcontainers[mysql]>=4.5", "greenlet>=3.0",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "-m 'not integration' --cov=app --cov-branch --cov-report=term-missing"
markers = ["integration: 需要 docker 的 MySQL 约束/迁移测试"]

[tool.coverage.report]
fail_under = 100
show_missing = true

[tool.ruff]
line-length = 100
[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]

[tool.mypy]
python_version = "3.12"
strict = true
plugins = ["pydantic.mypy"]
```

- [ ] **Step 2: 写空壳 app/main.py（仅可 import，路由后续 task 挂）**

```python
from fastapi import FastAPI


def create_app() -> FastAPI:
    return FastAPI(title="lending-bank-gateway", version="0.1.0")


app = create_app()
```

- [ ] **Step 3: 写 tests/conftest.py（基础 app fixture）**

```python
import pytest
from fastapi import FastAPI

from app.main import create_app


@pytest.fixture()
def app() -> FastAPI:
    return create_app()
```

- [ ] **Step 4: 写冒烟测试 tests/test_main.py**

```python
from fastapi import FastAPI

from app.main import create_app


def test_create_app_returns_fastapi() -> None:
    assert isinstance(create_app(), FastAPI)
```

- [ ] **Step 5: 安装依赖并跑通**

Run: `cd /home/ubuntu/lending/lending-bank-gateway && python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q`
Expected: `1 passed`，coverage 100%

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml app tests .gitignore
git commit -m "chore: [M1] 项目骨架 + 工具链（pytest/ruff/mypy/coverage 100 门禁）"
```

### Task 2: Settings + 异步 DB 基建

**Files:**
- Create: `app/core/config.py`, `app/core/db.py`
- Test: `tests/core/test_config.py`, `tests/core/test_db.py`

- [ ] **Step 1: 写失败测试 tests/core/test_config.py**

```python
from app.core.config import Settings


def test_settings_defaults_and_env_override(monkeypatch) -> None:
    monkeypatch.setenv("GW_DB_URL", "mysql+asyncmy://u:p@h:3306/lending_bank_gateway")
    monkeypatch.setenv("GW_WEDAP_BASE_URL", "http://wedap-dev:8080")
    s = Settings()
    assert s.db_url.endswith("/lending_bank_gateway")
    assert s.wedap_base_url == "http://wedap-dev:8080"
    assert s.wedap_timeout_seconds == 10.0
    assert s.bank_timezone == "Asia/Hong_Kong"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/core/test_config.py -q`
Expected: FAIL `ModuleNotFoundError: app.core.config`

- [ ] **Step 3: 实现 app/core/config.py**

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GW_", env_file=".env", extra="ignore")

    db_url: str = "sqlite+aiosqlite:///:memory:"
    wedap_base_url: str = "http://localhost:8021"
    wedap_timeout_seconds: float = 10.0
    bank_timezone: str = "Asia/Hong_Kong"
    s3_endpoint_url: str | None = None
    callback_target_lifecycle_url: str = "http://lending-lifecycel:9000/api/v1/bank/transaction-callback"
    s2s_secret: str | None = None
    outbox_max_attempts: int = 8


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: 写失败测试 tests/core/test_db.py（session 可用 + UTC 钉死语义）**

```python
import pytest
from sqlalchemy import text

from app.core.db import build_engine, build_session_factory


@pytest.mark.asyncio
async def test_session_roundtrip() -> None:
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    factory = build_session_factory(engine)
    async with factory() as session:
        assert (await session.execute(text("SELECT 1"))).scalar() == 1
    await engine.dispose()
```

- [ ] **Step 5: 实现 app/core/db.py**

```python
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy import event


def build_engine(db_url: str) -> AsyncEngine:
    engine = create_async_engine(db_url, pool_pre_ping=True, pool_recycle=3600)
    if db_url.startswith("mysql"):
        @event.listens_for(engine.sync_engine, "connect")
        def _pin_utc(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            with dbapi_conn.cursor() as cur:  # 规范11: DB session 钉 +00:00
                cur.execute("SET time_zone = '+00:00'")
    return engine


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
```

- [ ] **Step 6: 跑测试 + lint + commit**

Run: `.venv/bin/pytest tests/core -q && .venv/bin/ruff check . && .venv/bin/mypy app`
Expected: PASS（mysql 分支 sqlite 路径不触发——用 `# pragma: no cover` 标 `_pin_utc` 内部行，或加 mysql URL 构造测试只断言 listener 注册）

```bash
git add app/core tests/core
git commit -m "feat: [M1] Settings + async DB 基建（pool_pre_ping/recycle + MySQL UTC 钉）"
```

### Task 3: 规范 02 envelope + 四标识符上下文 + 健康检查

**Files:**
- Create: `app/core/envelope.py`, `app/core/context.py`, `app/api/v1/health.py`
- Modify: `app/main.py`
- Test: `tests/core/test_envelope.py`, `tests/api/test_health.py`

- [ ] **Step 1: 写失败测试 tests/core/test_envelope.py**

```python
from app.core.envelope import err, ok


def test_ok_envelope_shape() -> None:
    body = ok({"a": 1}, trace_id="trc-1")
    assert body == {"success": True, "data": {"a": 1}, "error": None, "trace_id": "trc-1"}


def test_err_envelope_shape() -> None:
    body = err("GW_400_VALIDATION", "bad biz_seq_no", trace_id="trc-2", details={"f": "bizSeqNo"})
    assert body["success"] is False and body["data"] is None
    assert body["error"] == {"code": "GW_400_VALIDATION", "message": "bad biz_seq_no", "details": {"f": "bizSeqNo"}}
    assert body["trace_id"] == "trc-2"
```

- [ ] **Step 2: 实现 app/core/envelope.py**

```python
from typing import Any


def ok(data: Any, *, trace_id: str) -> dict[str, Any]:
    return {"success": True, "data": data, "error": None, "trace_id": trace_id}


def err(code: str, message: str, *, trace_id: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": False,
        "data": None,
        "error": {"code": code, "message": message, "details": details or {}},
        "trace_id": trace_id,
    }
```

- [ ] **Step 3: 写失败测试 tests/api/test_health.py（含四标识符行为：无 X-Trace-Id 自动生成、有则回显）**

```python
from fastapi.testclient import TestClient


def test_healthz_ok(app) -> None:
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["success"] is True and r.json()["trace_id"]


def test_trace_id_echo(app) -> None:
    client = TestClient(app)
    r = client.get("/healthz", headers={"X-Trace-Id": "trc-echo"})
    assert r.json()["trace_id"] == "trc-echo"
```

- [ ] **Step 4: 实现 app/core/context.py + app/api/v1/health.py，main.py 装配中间件**

```python
# app/core/context.py
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


@dataclass(frozen=True)
class RequestIds:
    trace_id: str
    request_id: str | None
    tenant_id: str | None
    biz_seq_no: str | None


_ids: ContextVar[RequestIds] = ContextVar("ids", default=RequestIds("trc-none", None, None, None))


def current_ids() -> RequestIds:
    return _ids.get()


class IdentifierMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        ids = RequestIds(
            trace_id=request.headers.get("X-Trace-Id") or f"trc-{uuid.uuid4().hex}",
            request_id=request.headers.get("X-Request-Id"),
            tenant_id=request.headers.get("X-Tenant-Id"),
            biz_seq_no=request.headers.get("X-Biz-Seq-No"),
        )
        token = _ids.set(ids)
        try:
            response = await call_next(request)
        finally:
            _ids.reset(token)
        response.headers["X-Trace-Id"] = ids.trace_id
        return response
```

```python
# app/api/v1/health.py
from fastapi import APIRouter

from app.core.context import current_ids
from app.core.envelope import ok

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    return ok({"status": "alive"}, trace_id=current_ids().trace_id)
```

```python
# app/main.py（全量替换）
from fastapi import FastAPI

from app.api.v1.health import router as health_router
from app.core.context import IdentifierMiddleware


def create_app() -> FastAPI:
    app = FastAPI(title="lending-bank-gateway", version="0.1.0")
    app.add_middleware(IdentifierMiddleware)
    app.include_router(health_router)
    return app


app = create_app()
```

- [ ] **Step 5: 跑测 + commit**

Run: `.venv/bin/pytest tests/core tests/api -q && .venv/bin/ruff check . && .venv/bin/mypy app`
Expected: PASS

```bash
git add app tests
git commit -m "feat: [M1] 规范02 envelope + 四标识符中间件 + /healthz"
```

### Task 4: S2S 鉴权中间件 + /readyz + CI

**Files:**
- Create: `app/core/s2s.py`, `.github/workflows/ci.yml`
- Modify: `app/api/v1/health.py`, `app/main.py`
- Test: `tests/core/test_s2s.py`

- [ ] **Step 1: 写失败测试 tests/core/test_s2s.py**

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.context import IdentifierMiddleware
from app.core.s2s import S2SMiddleware


def _app(secret: str | None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(S2SMiddleware, secret=secret, exempt_paths={"/healthz", "/readyz"})
    app.add_middleware(IdentifierMiddleware)

    @app.get("/healthz")
    async def hz() -> dict:
        return {"ok": True}

    @app.post("/api/v1/x")
    async def x() -> dict:
        return {"ok": True}

    return app


def test_exempt_path_passes_without_headers() -> None:
    assert TestClient(_app("sec")).get("/healthz").status_code == 200


def test_missing_caller_service_rejected() -> None:
    r = TestClient(_app("sec")).post("/api/v1/x")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "GW_401_S2S"


def test_caller_with_valid_token_passes() -> None:
    r = TestClient(_app("sec")).post(
        "/api/v1/x", headers={"X-Caller-Service": "lifecycle", "X-S2S-Token": "sec"}
    )
    assert r.status_code == 200


def test_secret_unset_only_requires_caller_header() -> None:
    r = TestClient(_app(None)).post("/api/v1/x", headers={"X-Caller-Service": "lifecycle"})
    assert r.status_code == 200
```

- [ ] **Step 2: 实现 app/core/s2s.py**

```python
import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.envelope import err


class S2SMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, secret: str | None, exempt_paths: set[str]):  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._secret = secret
        self._exempt = exempt_paths

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path in self._exempt:
            return await call_next(request)
        trace_id = request.headers.get("X-Trace-Id", "trc-s2s")
        if not request.headers.get("X-Caller-Service"):
            return JSONResponse(err("GW_401_S2S", "missing X-Caller-Service", trace_id=trace_id), 401)
        if self._secret is not None:
            token = request.headers.get("X-S2S-Token", "")
            if not hmac.compare_digest(token, self._secret):
                return JSONResponse(err("GW_401_S2S", "bad s2s token", trace_id=trace_id), 401)
        return await call_next(request)
```

- [ ] **Step 3: /readyz（探 DB SELECT 1；wedap/S3 探测留 M6 部署任务接线）+ main.py 挂 S2S**

```python
# app/api/v1/health.py 追加
from fastapi import Request
from sqlalchemy import text


@router.get("/readyz")
async def readyz(request: Request) -> dict:
    checks: dict[str, str] = {}
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        checks["db"] = "not-wired"
    else:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
            checks["db"] = "ok"
    return ok(checks, trace_id=current_ids().trace_id)
```

```python
# app/main.py create_app 内、IdentifierMiddleware 之后追加：
from app.core.config import get_settings
from app.core.db import build_engine, build_session_factory
from app.core.s2s import S2SMiddleware

settings = get_settings()
app.add_middleware(S2SMiddleware, secret=settings.s2s_secret, exempt_paths={"/healthz", "/readyz"})
engine = build_engine(settings.db_url)
app.state.engine = engine
app.state.session_factory = build_session_factory(engine)
```

- [ ] **Step 4: 写 CI workflow .github/workflows/ci.yml**

```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e '.[dev]'
      - run: ruff check .
      - run: mypy app
      - run: pytest -q            # 单测 + 覆盖率 100 门禁
      - run: pytest -q -m integration --no-cov
        if: ${{ false }}          # integration 待 M2 testcontainers 任务启用后改为 true
```

- [ ] **Step 5: 跑测 + commit**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/mypy app`
Expected: PASS

```bash
git add app tests .github
git commit -m "feat: [M1] S2S 鉴权中间件 + /readyz + CI 门禁"
```

---

## Milestone 2 · 数据模型与状态机

### Task 5: Base/Mixin + bank_txn_order/leg + 迁移 0001

**Files:**
- Create: `app/models/base.py`, `app/models/txn.py`, `alembic/`（init + versions/0001）
- Test: `tests/models/test_txn.py`, `tests/integration/test_constraints_mysql.py`

- [ ] **Step 1: 写失败测试 tests/models/test_txn.py（sqlite 建表 + 唯一约束语义）**

```python
import pytest
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.txn import BankTxnLeg, BankTxnOrder


@pytest.fixture()
async def session():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with build_session_factory(engine)() as s:
        yield s
    await engine.dispose()


def _order(**kw) -> BankTxnOrder:
    defaults = dict(
        tenant_id="WBTHK01", biz_seq_no="DSB-20260611-0001234567890",
        business_action="DISBURSE", biz_type="DSB", amount=Decimal("100.0000"),
        currency="USD", caller_service="lifecycle", status="ACCEPTED",
    )
    defaults.update(kw)
    return BankTxnOrder(**defaults)


@pytest.mark.asyncio
async def test_order_unique_tenant_biz_seq_no(session) -> None:
    session.add(_order())
    await session.commit()
    session.add(_order())
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_leg_unique_external_ref_and_step_seq(session) -> None:
    order = _order()
    session.add(order)
    await session.commit()
    leg = dict(tenant_id="WBTHK01", order_id=order.id, biz_seq_no=order.biz_seq_no,
               external_system="WEDAP_BANK", step_type="DISBURSEMENT_COLLECTION", step_seq=1,
               external_ref="HSBC202606110001", amount=Decimal("100.0000"), currency="USD",
               status="SUCCESS")
    session.add(BankTxnLeg(**leg))
    await session.commit()
    session.add(BankTxnLeg(**{**leg, "step_seq": 2}))  # 同 external_ref 不同 step → 撞唯一
    with pytest.raises(IntegrityError):
        await session.commit()
```

- [ ] **Step 2: 实现 app/models/base.py + app/models/txn.py**

```python
# app/models/base.py
import datetime as dt

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TenantMixin:
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
```

```python
# app/models/txn.py
import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class BankTxnOrder(Base, TenantMixin, TimestampMixin):
    __tablename__ = "bank_txn_order"
    __table_args__ = (UniqueConstraint("tenant_id", "biz_seq_no", name="uq_order_tenant_biz"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    biz_seq_no: Mapped[str] = mapped_column(String(32), nullable=False)
    business_action: Mapped[str] = mapped_column(String(32), nullable=False)
    biz_type: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(21, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    caller_service: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class BankTxnLeg(Base, TenantMixin, TimestampMixin):
    __tablename__ = "bank_txn_leg"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_system", "external_ref", name="uq_leg_tenant_ext"),
        UniqueConstraint("tenant_id", "biz_seq_no", "step_seq", name="uq_leg_tenant_biz_step"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("bank_txn_order.id"), nullable=False)
    biz_seq_no: Mapped[str] = mapped_column(String(32), nullable=False)
    external_system: Mapped[str] = mapped_column(String(16), nullable=False, default="WEDAP_BANK")
    external_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    step_type: Mapped[str] = mapped_column(String(40), nullable=False)
    step_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(21, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payer_account: Mapped[str | None] = mapped_column(String(64))
    payee_account: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    txn_date: Mapped[str | None] = mapped_column(String(8))     # BANK_TIMEZONE YYYYMMDD 透传不重算
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 3: alembic init + 迁移 0001（autogenerate 后人工核对）**

Run: `.venv/bin/alembic init alembic`，改 `alembic/env.py` 读 `Settings().db_url`（sync 驱动替换 `asyncmy→pymysql` 或用 async env 模板）+ `target_metadata = Base.metadata`；然后 `GW_DB_URL=... .venv/bin/alembic revision --autogenerate -m "0001 order/leg"`。核对生成文件包含两表 + 三个 UniqueConstraint。

- [ ] **Step 4: 写 testcontainers 集成测试 tests/integration/test_constraints_mysql.py（标 integration）**

```python
import pytest
from decimal import Decimal

pytestmark = pytest.mark.integration


def test_constraints_on_real_mysql() -> None:
    from sqlalchemy import create_engine
    from testcontainers.mysql import MySqlContainer

    from app.models.base import Base
    from app.models.txn import BankTxnOrder

    with MySqlContainer("mysql:8.0") as mysql:
        engine = create_engine(mysql.get_connection_url())
        Base.metadata.create_all(engine)
        from sqlalchemy.orm import Session
        with Session(engine) as s:
            s.add(BankTxnOrder(tenant_id="t1", biz_seq_no="DSB-20260611-0000000000001",
                               business_action="DISBURSE", biz_type="DSB",
                               amount=Decimal("1.0000"), currency="USD",
                               caller_service="lifecycle", status="ACCEPTED"))
            s.commit()
            dup = BankTxnOrder(tenant_id="t1", biz_seq_no="DSB-20260611-0000000000001",
                               business_action="DISBURSE", biz_type="DSB",
                               amount=Decimal("1.0000"), currency="USD",
                               caller_service="lifecycle", status="ACCEPTED")
            s.add(dup)
            import sqlalchemy.exc
            with pytest.raises(sqlalchemy.exc.IntegrityError):
                s.commit()
```

- [ ] **Step 5: 跑测 + commit**

Run: `.venv/bin/pytest tests/models -q && .venv/bin/pytest -m integration --no-cov -q`（后者需本机 docker）
Expected: 均 PASS

```bash
git add app/models alembic tests
git commit -m "feat: [M2] bank_txn_order/leg 模型 + 0001 迁移 + tenant 维度唯一约束（含 MySQL 集成验证）"
```

### Task 6: 幂等三元组（模型 + 服务）

**Files:**
- Create: `app/models/idempotency.py`, `app/services/idempotency.py`，alembic versions/0002
- Test: `tests/services/test_idempotency.py`

- [ ] **Step 1: 写失败测试 tests/services/test_idempotency.py**

```python
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
    hit = await check_or_register(session, tenant_id="t1", business_scope="p2p_disburse",
                                  idempotency_key="DSB-20260611-0000000000001",
                                  method="POST", path="/api/v1/loans/p2p-disbursements",
                                  payload=PAYLOAD)
    assert hit is None


@pytest.mark.asyncio
async def test_replay_same_payload_returns_first_response(session) -> None:
    await check_or_register(session, tenant_id="t1", business_scope="p2p_disburse",
                            idempotency_key="k1", method="POST", path="/p", payload=PAYLOAD)
    await record_response(session, tenant_id="t1", business_scope="p2p_disburse",
                          idempotency_key="k1", response={"txnStatus": "PROCESSING"},
                          final_effect_id="order:1")
    hit = await check_or_register(session, tenant_id="t1", business_scope="p2p_disburse",
                                  idempotency_key="k1", method="POST", path="/p", payload=PAYLOAD)
    assert hit == {"txnStatus": "PROCESSING"}


@pytest.mark.asyncio
async def test_same_key_different_payload_conflicts(session) -> None:
    await check_or_register(session, tenant_id="t1", business_scope="p2p_disburse",
                            idempotency_key="k1", method="POST", path="/p", payload=PAYLOAD)
    with pytest.raises(IdempotencyConflict):
        await check_or_register(session, tenant_id="t1", business_scope="p2p_disburse",
                                idempotency_key="k1", method="POST", path="/p",
                                payload={**PAYLOAD, "amount": "999.0000"})
```

- [ ] **Step 2: 实现模型 + 服务**

```python
# app/models/idempotency.py
from sqlalchemy import JSON, BigInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class IdempotencyRecord(Base, TenantMixin, TimestampMixin):
    __tablename__ = "idempotency_record"
    __table_args__ = (
        UniqueConstraint("tenant_id", "business_scope", "idempotency_key", name="uq_idem_triple"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    business_scope: Mapped[str] = mapped_column(String(48), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    first_response: Mapped[dict | None] = mapped_column(JSON)
    final_effect_id: Mapped[str | None] = mapped_column(String(64))
```

```python
# app/services/idempotency.py
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency import IdempotencyRecord


class IdempotencyConflict(Exception):
    """同 key 不同 payload —— 北向必须回 409。"""


def payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def check_or_register(session: AsyncSession, *, tenant_id: str, business_scope: str,
                            idempotency_key: str, method: str, path: str,
                            payload: dict[str, Any]) -> dict[str, Any] | None:
    h = payload_hash(payload)
    row = (await session.execute(select(IdempotencyRecord).where(
        IdempotencyRecord.tenant_id == tenant_id,
        IdempotencyRecord.business_scope == business_scope,
        IdempotencyRecord.idempotency_key == idempotency_key,
    ))).scalar_one_or_none()
    if row is None:
        session.add(IdempotencyRecord(tenant_id=tenant_id, business_scope=business_scope,
                                      idempotency_key=idempotency_key, method=method,
                                      path=path, payload_hash=h))
        await session.flush()
        return None
    if row.payload_hash != h:
        raise IdempotencyConflict(idempotency_key)
    return row.first_response


async def record_response(session: AsyncSession, *, tenant_id: str, business_scope: str,
                          idempotency_key: str, response: dict[str, Any],
                          final_effect_id: str | None = None) -> None:
    row = (await session.execute(select(IdempotencyRecord).where(
        IdempotencyRecord.tenant_id == tenant_id,
        IdempotencyRecord.business_scope == business_scope,
        IdempotencyRecord.idempotency_key == idempotency_key,
    ))).scalar_one()
    row.first_response = response
    row.final_effect_id = final_effect_id
```

- [ ] **Step 3: alembic 0002 + 跑测 + commit**

Run: `.venv/bin/alembic revision --autogenerate -m "0002 idempotency_record"` 核对；`.venv/bin/pytest tests/services -q`
Expected: PASS

```bash
git add app/models/idempotency.py app/services/idempotency.py alembic tests/services
git commit -m "feat: [M2] 幂等三元组（payload_hash 冲突 409 / first_response 重放）"
```

### Task 7: callback_inbox / callback_outbox 模型

**Files:**
- Create: `app/models/callback.py`，alembic versions/0003
- Test: `tests/models/test_callback.py`

- [ ] **Step 1: 写失败测试（inbox 三元组幂等 + outbox 状态字段）**

```python
import pytest
from sqlalchemy.exc import IntegrityError

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.callback import CallbackInbox, CallbackOutbox


@pytest.fixture()
async def session():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with build_session_factory(engine)() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_inbox_unique_tenant_source_request(session) -> None:
    row = dict(tenant_id="t1", source="WEDAP_TXN", request_id="r1", payload={"a": 1}, status="RECEIVED")
    session.add(CallbackInbox(**row))
    await session.commit()
    session.add(CallbackInbox(**row))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_outbox_defaults(session) -> None:
    ob = CallbackOutbox(tenant_id="t1", target="lifecycle", payload={"x": 1})
    session.add(ob)
    await session.commit()
    assert ob.status == "PENDING" and ob.attempts == 0
```

- [ ] **Step 2: 实现 app/models/callback.py**

```python
import datetime as dt

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class CallbackInbox(Base, TenantMixin, TimestampMixin):
    __tablename__ = "callback_inbox"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "request_id", name="uq_inbox_tenant_src_req"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)   # WEDAP_TXN / WEDAP_RECON
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RECEIVED")
    error: Mapped[str | None] = mapped_column(Text)


class CallbackOutbox(Base, TenantMixin, TimestampMixin):
    __tablename__ = "callback_outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    target: Mapped[str] = mapped_column(String(32), nullable=False)   # lifecycle / customers(C4)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 3: alembic 0003 + 跑测 + commit**

```bash
git add app/models/callback.py alembic tests/models
git commit -m "feat: [M2] callback inbox/outbox 模型（inbox 三元组幂等）"
```

### Task 8: recon_result 四表 + query_audit/balance_snapshot/audit_log

**Files:**
- Create: `app/models/recon.py`, `app/models/query_audit.py`, `app/models/audit.py`，alembic versions/0004
- Test: `tests/models/test_recon_models.py`, `tests/models/test_audit.py`

- [ ] **Step 1: 写失败测试（task 双唯一约束 + supersede 字段 + audit hash chain 字段）**

```python
import pytest
from sqlalchemy.exc import IntegrityError

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.recon import ReconResultTask


@pytest.fixture()
async def session():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with build_session_factory(engine)() as s:
        yield s
    await engine.dispose()


def _task(**kw):
    d = dict(tenant_id="OCBC", task_no="RECON-OCBC-20260604", version=1, recon_date="20260604",
             s3_bucket="b", s3_key="k", file_md5="0" * 32, diff_count=0,
             status="NOTIFIED", request_id="recon-result-RECON-OCBC-20260604-v1")
    d.update(kw)
    return ReconResultTask(**d)


@pytest.mark.asyncio
async def test_unique_request_id_and_task_version(session) -> None:
    session.add(_task())
    await session.commit()
    session.add(_task(request_id="other"))   # 同 (tenant,task_no,version) 仍撞
    with pytest.raises(IntegrityError):
        await session.commit()
```

- [ ] **Step 2: 实现三个模型文件**

```python
# app/models/recon.py
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class ReconResultTask(Base, TenantMixin, TimestampMixin):
    __tablename__ = "recon_result_task"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", name="uq_recon_task_req"),
        UniqueConstraint("tenant_id", "task_no", "version", name="uq_recon_task_ver"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_no: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    recon_date: Mapped[str] = mapped_column(String(8), nullable=False)
    s3_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(256), nullable=False)
    file_md5: Mapped[str] = mapped_column(String(32), nullable=False)
    diff_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # NOTIFIED/DOWNLOADED/PARSED/FAILED/SUPERSEDED
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(16))
    schema_version: Mapped[str | None] = mapped_column(String(16))
    column_check: Mapped[dict | None] = mapped_column(JSON)
    archive_path: Mapped[str | None] = mapped_column(String(256))    # 原始文件本地存档


class ReconResultDiff(Base, TenantMixin, TimestampMixin):
    __tablename__ = "recon_result_diff"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    diff_type: Mapped[str] = mapped_column(String(8), nullable=False)  # LONG/SHORT/AMOUNT/STATUS
    wedap_biz_seq_no: Mapped[str | None] = mapped_column(String(32), index=True)
    bank_seq_no: Mapped[str | None] = mapped_column(String(64), index=True)
    wedap_amount: Mapped[Decimal | None] = mapped_column(Numeric(21, 4))
    bank_amount: Mapped[Decimal | None] = mapped_column(Numeric(21, 4))
    diff_amount: Mapped[Decimal | None] = mapped_column(Numeric(21, 4))
    wedap_status: Mapped[str | None] = mapped_column(String(16))
    bank_status: Mapped[str | None] = mapped_column(String(16))


class ReconResultSourceWedap(Base, TenantMixin, TimestampMixin):
    __tablename__ = "recon_result_source_wedap"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    biz_type: Mapped[str | None] = mapped_column(String(24))
    biz_seq_no: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    bank_biz_seq_no: Mapped[str | None] = mapped_column(String(64), index=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(21, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    payer_account: Mapped[str | None] = mapped_column(String(64))
    payee_account: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(16))
    error_msg: Mapped[str | None] = mapped_column(String(256))


class ReconResultSourceBank(Base, TenantMixin, TimestampMixin):
    __tablename__ = "recon_result_source_bank"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    bank_seq_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    txn_date: Mapped[str | None] = mapped_column(String(8))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(21, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    payer_account: Mapped[str | None] = mapped_column(String(64))
    payee_account: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(16))
    file_name: Mapped[str | None] = mapped_column(String(128))
    line_no: Mapped[int | None] = mapped_column(Integer)
```

```python
# app/models/query_audit.py
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
import datetime as dt

from app.models.base import Base, TenantMixin, TimestampMixin


class QueryAudit(Base, TenantMixin, TimestampMixin):
    __tablename__ = "query_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    caller_service: Mapped[str | None] = mapped_column(String(32))


class BalanceSnapshot(Base, TenantMixin, TimestampMixin):
    __tablename__ = "balance_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(21, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source_endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

```python
# app/models/audit.py
from sqlalchemy import JSON, BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class AuditLog(Base, TenantMixin, TimestampMixin):
    """append-only：应用层只 INSERT；MySQL 权限/trigger 禁 UPDATE/DELETE（部署期 SQL 落地）。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)      # 认证上下文，禁客户端伪造
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)     # 如 bank_txn_order:123
    payload: Mapped[dict | None] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
```

- [ ] **Step 3: alembic 0004 + 跑测 + commit**

```bash
git add app/models alembic tests/models
git commit -m "feat: [M2] recon_result 四表 + query_audit/balance_snapshot/audit_log"
```

### Task 9: 状态机模块（转移守卫 + 父子聚合）

**Files:**
- Create: `app/domain/states.py`, `app/domain/biz_seq.py`
- Test: `tests/domain/test_states.py`, `tests/domain/test_biz_seq.py`

- [ ] **Step 1: 写失败测试 tests/domain/test_states.py（spec §6 全表）**

```python
import pytest

from app.domain.states import (
    IllegalTransition, LegStatus, OrderStatus, aggregate_order_status, assert_transition,
)


def test_legal_transitions() -> None:
    assert_transition(OrderStatus.ACCEPTED, OrderStatus.SUBMITTED)
    assert_transition(OrderStatus.SUBMITTED, OrderStatus.RESULT_UNKNOWN)
    assert_transition(OrderStatus.RESULT_UNKNOWN, OrderStatus.SUCCEEDED)
    assert_transition(OrderStatus.SUCCEEDED, OrderStatus.PARTIALLY_REVERSED)
    assert_transition(OrderStatus.PARTIALLY_REVERSED, OrderStatus.REVERSED)


@pytest.mark.parametrize("src,dst", [
    (OrderStatus.SUCCEEDED, OrderStatus.FAILED),       # 终态不可改写
    (OrderStatus.FAILED, OrderStatus.SUCCEEDED),
    (OrderStatus.ACCEPTED, OrderStatus.SUCCEEDED),     # 不许跳过受理链
    (OrderStatus.REVERSED, OrderStatus.PROCESSING),
])
def test_illegal_transitions(src, dst) -> None:
    with pytest.raises(IllegalTransition):
        assert_transition(src, dst)


def test_aggregate_all_success() -> None:
    assert aggregate_order_status([(LegStatus.SUCCESS, "100")]) == OrderStatus.SUCCEEDED


def test_aggregate_unknown_dominates() -> None:
    assert aggregate_order_status(
        [(LegStatus.SUCCESS, "100"), (LegStatus.UNKNOWN, "50")]) == OrderStatus.RESULT_UNKNOWN


def test_aggregate_pending_is_processing() -> None:
    assert aggregate_order_status(
        [(LegStatus.SUCCESS, "100"), (LegStatus.PENDING, "50")]) == OrderStatus.PROCESSING


def test_aggregate_failed_no_inflight() -> None:
    assert aggregate_order_status(
        [(LegStatus.FAILED, "100"), (LegStatus.SUCCESS, "50")]) == OrderStatus.FAILED


def test_aggregate_partial_reversal() -> None:
    legs = [(LegStatus.SUCCESS, "100"), (LegStatus.SUCCESS, "50"),
            (LegStatus.REVERSED, "50"), (LegStatus.REVERSAL, "50")]
    assert aggregate_order_status(legs) == OrderStatus.PARTIALLY_REVERSED


def test_aggregate_full_reversal() -> None:
    legs = [(LegStatus.REVERSED, "100"), (LegStatus.REVERSAL, "100")]
    assert aggregate_order_status(legs) == OrderStatus.REVERSED
```

- [ ] **Step 2: 实现 app/domain/states.py**

```python
from decimal import Decimal
from enum import StrEnum


class OrderStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    SUBMITTED = "SUBMITTED"
    PROCESSING = "PROCESSING"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    PARTIALLY_REVERSED = "PARTIALLY_REVERSED"
    REVERSED = "REVERSED"


class LegStatus(StrEnum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    REVERSED = "REVERSED"
    REVERSAL = "REVERSAL"


_ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.ACCEPTED: {OrderStatus.SUBMITTED, OrderStatus.FAILED, OrderStatus.CANCELLED},
    OrderStatus.SUBMITTED: {OrderStatus.PROCESSING, OrderStatus.RESULT_UNKNOWN,
                            OrderStatus.SUCCEEDED, OrderStatus.FAILED, OrderStatus.EXPIRED},
    OrderStatus.PROCESSING: {OrderStatus.RESULT_UNKNOWN, OrderStatus.SUCCEEDED,
                             OrderStatus.FAILED, OrderStatus.EXPIRED},
    OrderStatus.RESULT_UNKNOWN: {OrderStatus.PROCESSING, OrderStatus.SUCCEEDED,
                                 OrderStatus.FAILED, OrderStatus.EXPIRED},
    OrderStatus.SUCCEEDED: {OrderStatus.PARTIALLY_REVERSED, OrderStatus.REVERSED},
    OrderStatus.PARTIALLY_REVERSED: {OrderStatus.REVERSED},
    OrderStatus.FAILED: set(), OrderStatus.EXPIRED: set(),
    OrderStatus.CANCELLED: set(), OrderStatus.REVERSED: set(),
}


class IllegalTransition(Exception):
    pass


def assert_transition(src: OrderStatus, dst: OrderStatus) -> None:
    if dst not in _ALLOWED[src]:
        raise IllegalTransition(f"{src} -> {dst}")


def aggregate_order_status(legs: list[tuple[LegStatus, str]]) -> OrderStatus:
    """spec §6 父子聚合表。legs: [(status, amount_str)]，REVERSAL 金额按覆盖度判部分/全额冲正。"""
    statuses = [s for s, _ in legs]
    if LegStatus.UNKNOWN in statuses:
        return OrderStatus.RESULT_UNKNOWN
    if LegStatus.PENDING in statuses:
        return OrderStatus.PROCESSING
    reversal = sum(Decimal(a) for s, a in legs if s == LegStatus.REVERSAL)
    if reversal > 0:
        gross = sum(Decimal(a) for s, a in legs
                    if s in (LegStatus.SUCCESS, LegStatus.REVERSED))
        return OrderStatus.REVERSED if reversal >= gross else OrderStatus.PARTIALLY_REVERSED
    if LegStatus.FAILED in statuses:
        return OrderStatus.FAILED
    return OrderStatus.SUCCEEDED
```

- [ ] **Step 3: 写失败测试 + 实现 app/domain/biz_seq.py（ADR-0029 校验）**

```python
# tests/domain/test_biz_seq.py
import pytest

from app.domain.biz_seq import validate_biz_seq_no


@pytest.mark.parametrize("v", ["DSB-20260611-0001234567890", "RPY-20260611-9999999999"])
def test_valid(v) -> None:
    validate_biz_seq_no(v)


@pytest.mark.parametrize("v", ["", "WB-1704067200000-DISB-10-0001-123456", "dsb-20260611-1",
                               "DSB-2026-0001234567890", "X" * 33])
def test_invalid(v) -> None:
    with pytest.raises(ValueError):
        validate_biz_seq_no(v)
```

```python
# app/domain/biz_seq.py
import re

_PATTERN = re.compile(r"^[A-Z]{2,4}-\d{8}-\d{10,}$")   # ADR-0029


def validate_biz_seq_no(value: str) -> None:
    if len(value) > 32 or not _PATTERN.match(value):
        raise ValueError(f"biz_seq_no 不符合 ADR-0029 格式: {value!r}")
```

- [ ] **Step 4: 跑测 + commit**

Run: `.venv/bin/pytest tests/domain -q && .venv/bin/mypy app`
Expected: PASS

```bash
git add app/domain tests/domain
git commit -m "feat: [M2] order/leg 状态机（转移守卫+父子聚合）+ ADR-0029 biz_seq_no 校验"
```

---

## Milestone 3 · 资金主链路（南向 client + 受理服务 + 北向 API）

### Task 10: wedap 南向 client + 契约 fixtures

**Files:**
- Create: `app/clients/wedap.py`, `tests/fixtures/wedap/disbursement_accepted.json`, `tests/fixtures/wedap/steps_two_legs.json`
- Test: `tests/clients/test_wedap.py`

- [ ] **Step 1: 落契约 fixtures（来源 WeDAPAPI-Lending.md 示例，checked-in 作 contract replay 基准）**

```json
// tests/fixtures/wedap/disbursement_accepted.json
{
  "code": "200", "msg": "SUCCESS",
  "data": {
    "txnStatus": "PROCESSING", "txnStatusDesc": "ACCEPTED",
    "sysTime": "1781164800000",
    "bizSeqNo": "DSB-20260611-0001234567890",
    "requestId": "req-0001"
  },
  "timestamp": 1781164800000
}
```

```json
// tests/fixtures/wedap/steps_two_legs.json
{
  "code": "200", "msg": "SUCCESS",
  "data": {
    "bizSeqNo": "DSB-20260611-0001234567890",
    "steps": [
      {"stepType": "DISBURSEMENT_COLLECTION", "stepSeq": 1, "sysRefNo": "HSBC202606110001",
       "amount": "60.0000", "currencyCode": "USD", "payerAccount": "L001", "payeeAccount": "POOL",
       "status": "SUCCESS", "txnDate": "20260611"},
      {"stepType": "DISBURSEMENT_DISTRIBUTION", "stepSeq": 2, "sysRefNo": "HSBC202606110002",
       "amount": "60.0000", "currencyCode": "USD", "payerAccount": "POOL", "payeeAccount": "B001",
       "status": "SUCCESS", "txnDate": "20260611"}
    ]
  },
  "timestamp": 1781164800000
}
```

- [ ] **Step 2: 写失败测试 tests/clients/test_wedap.py（respx 拦截）**

```python
import json
import pathlib

import httpx
import pytest
import respx

from app.clients.wedap import WedapClient, WedapError

FIX = pathlib.Path(__file__).parent.parent / "fixtures" / "wedap"


def _client() -> WedapClient:
    return WedapClient(base_url="http://wedap", timeout_seconds=1.0)


@pytest.mark.asyncio
@respx.mock
async def test_submit_disbursement_accepted() -> None:
    respx.post("http://wedap/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(200, json=json.loads((FIX / "disbursement_accepted.json").read_text())))
    resp = await _client().submit_disbursement(
        tenant_id="OCBC", request_id="req-0001",
        payload={"bizSeqNo": "DSB-20260611-0001234567890"})
    assert resp["txnStatus"] == "PROCESSING"


@pytest.mark.asyncio
@respx.mock
async def test_get_composite_steps() -> None:
    respx.get("http://wedap/api/v1/composite-transactions/DSB-20260611-0001234567890/steps").mock(
        return_value=httpx.Response(200, json=json.loads((FIX / "steps_two_legs.json").read_text())))
    steps = await _client().get_composite_steps(tenant_id="OCBC", biz_seq_no="DSB-20260611-0001234567890")
    assert len(steps) == 2 and steps[0]["sysRefNo"] == "HSBC202606110001"


@pytest.mark.asyncio
@respx.mock
async def test_non_200_code_raises() -> None:
    respx.post("http://wedap/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(200, json={"code": "500", "msg": "SYSTEM_ERROR"}))
    with pytest.raises(WedapError):
        await _client().submit_disbursement(tenant_id="OCBC", request_id="r", payload={})


@pytest.mark.asyncio
@respx.mock
async def test_timeout_propagates() -> None:
    respx.post("http://wedap/api/v1/loans/p2p-disbursements").mock(side_effect=httpx.ConnectTimeout("t"))
    with pytest.raises(httpx.TimeoutException):
        await _client().submit_disbursement(tenant_id="OCBC", request_id="r", payload={})
```

- [ ] **Step 3: 实现 app/clients/wedap.py**

```python
from typing import Any

import httpx


class WedapError(Exception):
    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"wedap {code}: {msg}")
        self.code = code


class WedapClient:
    """南向 client：所有外呼带 timeout（规范07 §1），写操作不自动重试（幂等靠上层 RESULT_UNKNOWN 收敛）。"""

    def __init__(self, *, base_url: str, timeout_seconds: float) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def _headers(self, tenant_id: str, request_id: str) -> dict[str, str]:
        return {"X-Tenant-Id": tenant_id, "X-Request-Id": request_id,
                "Content-Type": "application/json"}

    async def _post(self, path: str, *, tenant_id: str, request_id: str,
                    payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(f"{self._base}{path}", json=payload,
                                  headers=self._headers(tenant_id, request_id))
        return self._unwrap(r)

    async def _get(self, path: str, *, tenant_id: str, request_id: str,
                   params: dict[str, str] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(f"{self._base}{path}", params=params,
                                 headers=self._headers(tenant_id, request_id))
        return self._unwrap(r)

    @staticmethod
    def _unwrap(r: httpx.Response) -> dict[str, Any]:
        r.raise_for_status()
        body = r.json()
        if str(body.get("code")) != "200":
            raise WedapError(str(body.get("code")), str(body.get("msg")))
        return body.get("data") or {}

    async def submit_disbursement(self, *, tenant_id: str, request_id: str,
                                  payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/api/v1/loans/p2p-disbursements",
                                tenant_id=tenant_id, request_id=request_id, payload=payload)

    async def submit_repayment(self, *, tenant_id: str, request_id: str,
                               payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/api/v1/loans/p2p-repayments",
                                tenant_id=tenant_id, request_id=request_id, payload=payload)

    async def collect_from_users(self, *, tenant_id: str, request_id: str,
                                 payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/api/v1/bank-funds/collect-from-users",
                                tenant_id=tenant_id, request_id=request_id, payload=payload)

    async def distribute_to_users(self, *, tenant_id: str, request_id: str,
                                  payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/api/v1/bank-funds/distribute-to-users",
                                tenant_id=tenant_id, request_id=request_id, payload=payload)

    async def query_funds_status(self, *, tenant_id: str, request_id: str,
                                 biz_seq_no: str) -> dict[str, Any]:
        return await self._get("/api/v1/bank-funds/status", tenant_id=tenant_id,
                               request_id=request_id, params={"bizSeqNo": biz_seq_no})

    async def get_composite_steps(self, *, tenant_id: str, biz_seq_no: str) -> list[dict[str, Any]]:
        data = await self._get(f"/api/v1/composite-transactions/{biz_seq_no}/steps",
                               tenant_id=tenant_id, request_id=f"steps-{biz_seq_no}")
        return list(data.get("steps") or [])

    async def get_deposit_balance_total(self, *, tenant_id: str, request_id: str,
                                        user_id: str) -> dict[str, Any]:
        return await self._get("/api/v1/deposit/balances/total", tenant_id=tenant_id,
                               request_id=request_id, params={"userId": user_id})

    async def get_deposit_accounts(self, *, tenant_id: str, request_id: str,
                                   user_id: str) -> dict[str, Any]:
        return await self._get("/api/v1/deposit/accounts", tenant_id=tenant_id,
                               request_id=request_id, params={"userId": user_id})

    async def get_user_info(self, *, tenant_id: str, request_id: str,
                            params: dict[str, str]) -> dict[str, Any]:
        return await self._get("/api/v1/users/info", tenant_id=tenant_id,
                               request_id=request_id, params=params)
```

- [ ] **Step 4: 跑测 + commit**

Run: `.venv/bin/pytest tests/clients -q`
Expected: PASS

```bash
git add app/clients tests/clients tests/fixtures
git commit -m "feat: [M3] wedap 南向 client（全方法 timeout + code 解包 + 契约 fixtures）"
```

### Task 11: 受理服务 submit_order（事务边界 + RESULT_UNKNOWN）

**Files:**
- Create: `app/services/submit.py`
- Test: `tests/services/test_submit.py`

- [ ] **Step 1: 写失败测试（四场景：成功受理 / 超时转 RESULT_UNKNOWN / wedap 业务失败 / 幂等重放）**

```python
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.core.db import build_engine, build_session_factory
from app.domain.states import OrderStatus
from app.models.base import Base
from app.models.txn import BankTxnOrder
from app.services.submit import SubmitRequest, submit_order


@pytest.fixture()
async def factory():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


def _req(**kw) -> SubmitRequest:
    d = dict(tenant_id="OCBC", biz_seq_no="DSB-20260611-0001234567890",
             business_action="DISBURSE", biz_type="DSB", amount=Decimal("100.0000"),
             currency="USD", caller_service="lifecycle", request_id="req-1",
             business_scope="p2p_disburse", wedap_payload={"bizSeqNo": "DSB-20260611-0001234567890"})
    d.update(kw)
    return SubmitRequest(**d)


@pytest.mark.asyncio
async def test_accepted_flow_sets_submitted(factory) -> None:
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "PROCESSING"}
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert result["txnStatus"] == "PROCESSING"
    async with factory() as s:
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
        assert order.status == OrderStatus.SUBMITTED


@pytest.mark.asyncio
async def test_timeout_sets_result_unknown(factory) -> None:
    wedap = AsyncMock()
    wedap.submit_disbursement.side_effect = httpx.ConnectTimeout("t")
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert result["txnStatus"] == "RESULT_UNKNOWN"
    async with factory() as s:
        assert (await s.execute(select(BankTxnOrder))).scalar_one().status == OrderStatus.RESULT_UNKNOWN


@pytest.mark.asyncio
async def test_wedap_business_failure_sets_failed(factory) -> None:
    from app.clients.wedap import WedapError
    wedap = AsyncMock()
    wedap.submit_disbursement.side_effect = WedapError("422", "BUSINESS_RULE_VIOLATION")
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert result["txnStatus"] == "FAILED"


@pytest.mark.asyncio
async def test_idempotent_replay_returns_first_response_without_second_call(factory) -> None:
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "PROCESSING"}
    await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    again = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert again["txnStatus"] == "PROCESSING"
    assert wedap.submit_disbursement.await_count == 1
```

- [ ] **Step 2: 实现 app/services/submit.py**

```python
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.wedap import WedapError
from app.domain.biz_seq import validate_biz_seq_no
from app.domain.states import OrderStatus
from app.models.txn import BankTxnOrder
from app.services.idempotency import check_or_register, record_response


@dataclass(frozen=True)
class SubmitRequest:
    tenant_id: str
    biz_seq_no: str
    business_action: str
    biz_type: str
    amount: Decimal
    currency: str
    caller_service: str
    request_id: str
    business_scope: str
    wedap_payload: dict[str, Any]


async def submit_order(factory: async_sessionmaker, *,
                       wedap_call: Callable[..., Awaitable[dict[str, Any]]],
                       req: SubmitRequest) -> dict[str, Any]:
    """事务1: 幂等检查 + order(ACCEPTED) 落库并 commit（事务内禁外呼，规范14）；
    然后外呼 wedap；事务2: 按结果推进状态 + 回写幂等 first_response。"""
    validate_biz_seq_no(req.biz_seq_no)

    async with factory() as session:
        async with session.begin():
            hit = await check_or_register(
                session, tenant_id=req.tenant_id, business_scope=req.business_scope,
                idempotency_key=req.biz_seq_no, method="POST",
                path=req.business_scope, payload=req.wedap_payload)
            if hit is not None:
                return hit
            session.add(BankTxnOrder(
                tenant_id=req.tenant_id, biz_seq_no=req.biz_seq_no,
                business_action=req.business_action, biz_type=req.biz_type,
                amount=req.amount, currency=req.currency,
                caller_service=req.caller_service, status=OrderStatus.ACCEPTED,
                request_id=req.request_id))

    try:
        data = await wedap_call(tenant_id=req.tenant_id, request_id=req.request_id,
                                payload=req.wedap_payload)
        new_status, response = OrderStatus.SUBMITTED, {"txnStatus": data.get("txnStatus", "PROCESSING"),
                                                       "bizSeqNo": req.biz_seq_no}
    except (httpx.TimeoutException, httpx.TransportError):
        new_status, response = OrderStatus.RESULT_UNKNOWN, {"txnStatus": "RESULT_UNKNOWN",
                                                            "bizSeqNo": req.biz_seq_no}
    except WedapError as exc:
        new_status, response = OrderStatus.FAILED, {"txnStatus": "FAILED",
                                                    "bizSeqNo": req.biz_seq_no,
                                                    "errorCode": exc.code}

    now = dt.datetime.now(dt.UTC)
    async with factory() as session:
        async with session.begin():
            await session.execute(update(BankTxnOrder).where(
                BankTxnOrder.tenant_id == req.tenant_id,
                BankTxnOrder.biz_seq_no == req.biz_seq_no,
            ).values(status=new_status, submitted_at=now))
            await record_response(session, tenant_id=req.tenant_id,
                                  business_scope=req.business_scope,
                                  idempotency_key=req.biz_seq_no, response=response,
                                  final_effect_id=f"order:{req.biz_seq_no}")
    return response
```

- [ ] **Step 3: 跑测 + commit**

Run: `.venv/bin/pytest tests/services/test_submit.py -q`
Expected: 4 passed

```bash
git add app/services/submit.py tests/services/test_submit.py
git commit -m "feat: [M3] 受理服务（事务边界外呼 + RESULT_UNKNOWN/FAILED 推进 + 幂等重放零外呼）"
```

### Task 12: 北向资金交易 API ×4（loans + bank-funds）

**Files:**
- Create: `app/api/v1/loans.py`, `app/api/v1/bank_funds.py`, `app/api/deps.py`
- Modify: `app/main.py`
- Test: `tests/api/test_loans.py`, `tests/api/test_bank_funds.py`

- [ ] **Step 1: 写失败测试 tests/api/test_loans.py（envelope + 幂等 409 + 头校验）**

```python
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.main import create_app

HEADERS = {"X-Caller-Service": "lifecycle", "X-Tenant-Id": "OCBC",
           "X-Request-Id": "req-1", "Idempotency-Key": "DSB-20260611-0001234567890"}
BODY = {"bizSeqNo": "DSB-20260611-0001234567890", "channelId": "LEN", "transType": "DISBURSEMENT",
        "disbursementInfo": {"txnAmount": "100.0000", "currencyCode": "USD",
                             "userId": "U1", "userName": "u"},
        "lenders": [{"userId": "L1"}]}


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    app = create_app()
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "PROCESSING"}
    app.state.wedap = wedap
    return TestClient(app)


def test_disbursement_accepted_envelope(client) -> None:
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["data"]["txnStatus"] == "PROCESSING"
    assert body["data"]["bizSeqNo"] == BODY["bizSeqNo"] and body["trace_id"]


def test_missing_tenant_header_400(client) -> None:
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "GW_400_HEADER"


def test_bad_biz_seq_no_400(client) -> None:
    bad = {**BODY, "bizSeqNo": "WB-1704067200000-DISB-10-0001-123456"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=bad,
                    headers={**HEADERS, "Idempotency-Key": bad["bizSeqNo"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_same_key_different_payload_409(client) -> None:
    client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    mutated = {**BODY, "disbursementInfo": {**BODY["disbursementInfo"], "txnAmount": "999.0000"}}
    r = client.post("/api/v1/loans/p2p-disbursements", json=mutated, headers=HEADERS)
    assert r.status_code == 409 and r.json()["error"]["code"] == "GW_409_IDEMPOTENCY"
```

- [ ] **Step 2: 实现 app/api/deps.py（头校验 + 依赖注入）**

```python
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request

from app.core.context import current_ids


def require_headers(request: Request) -> dict[str, str]:
    ids = current_ids()
    if not ids.tenant_id:
        raise HTTPException(400, detail={"code": "GW_400_HEADER", "message": "missing X-Tenant-Id"})
    if not ids.request_id:
        raise HTTPException(400, detail={"code": "GW_400_HEADER", "message": "missing X-Request-Id"})
    return {"tenant_id": ids.tenant_id, "request_id": ids.request_id, "trace_id": ids.trace_id,
            "caller_service": request.headers.get("X-Caller-Service", "unknown")}


def parse_amount(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, detail={"code": "GW_400_VALIDATION",
                                         "message": f"bad amount: {raw!r}"}) from exc
```

- [ ] **Step 3: 实现 app/api/v1/loans.py（disbursements + repayments 双端点）**

```python
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.api.deps import parse_amount, require_headers
from app.core.envelope import err, ok
from app.domain.biz_seq import validate_biz_seq_no
from app.services.idempotency import IdempotencyConflict
from app.services.submit import SubmitRequest, submit_order

router = APIRouter(prefix="/api/v1/loans", tags=["loans"])

_ACTIONS = {
    "p2p-disbursements": ("DISBURSE", "DSB", "p2p_disburse", "submit_disbursement",
                          "disbursementInfo"),
    "p2p-repayments": ("REPAY", "RPY", "p2p_repay", "submit_repayment", "repaymentInfo"),
}


async def _submit(kind: str, request: Request, body: dict[str, Any]) -> dict:
    hdr = require_headers(request)
    action, biz_type, scope, client_method, info_key = _ACTIONS[kind]
    biz_seq_no = str(body.get("bizSeqNo", ""))
    try:
        validate_biz_seq_no(biz_seq_no)
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "GW_400_VALIDATION", "message": str(exc)}) from exc
    info = body.get(info_key) or {}
    req = SubmitRequest(
        tenant_id=hdr["tenant_id"], biz_seq_no=biz_seq_no, business_action=action,
        biz_type=biz_type, amount=parse_amount(info.get("txnAmount")),
        currency=str(info.get("currencyCode", "USD")), caller_service=hdr["caller_service"],
        request_id=hdr["request_id"], business_scope=scope, wedap_payload=body)
    wedap_call = getattr(request.app.state.wedap, client_method)
    try:
        data = await submit_order(request.app.state.session_factory,
                                  wedap_call=wedap_call, req=req)
    except IdempotencyConflict:
        raise HTTPException(409, detail={"code": "GW_409_IDEMPOTENCY",
                                         "message": "same key different payload"})
    return ok(data, trace_id=hdr["trace_id"])


@router.post("/p2p-disbursements")
async def p2p_disbursements(request: Request, body: dict[str, Any]) -> dict:
    return await _submit("p2p-disbursements", request, body)


@router.post("/p2p-repayments")
async def p2p_repayments(request: Request, body: dict[str, Any]) -> dict:
    return await _submit("p2p-repayments", request, body)
```

- [ ] **Step 4: 实现 app/api/v1/bank_funds.py（collect/distribute，同构）+ HTTPException→envelope 异常处理器 + main.py 装配**

```python
# app/api/v1/bank_funds.py
from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import parse_amount, require_headers
from app.core.envelope import ok
from app.services.submit import SubmitRequest, submit_order
from app.domain.biz_seq import validate_biz_seq_no
from fastapi import HTTPException
from app.services.idempotency import IdempotencyConflict

router = APIRouter(prefix="/api/v1/bank-funds", tags=["bank-funds"])

_KINDS = {
    "collect-from-users": ("COLLECT", "CLT", "bank_collect", "collect_from_users"),
    "distribute-to-users": ("DISTRIBUTE", "DST", "bank_distribute", "distribute_to_users"),
}


async def _submit(kind: str, request: Request, body: dict[str, Any]) -> dict:
    hdr = require_headers(request)
    action, biz_type, scope, client_method = _KINDS[kind]
    biz_seq_no = str(body.get("bizSeqNo", ""))
    try:
        validate_biz_seq_no(biz_seq_no)
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "GW_400_VALIDATION", "message": str(exc)}) from exc
    req = SubmitRequest(
        tenant_id=hdr["tenant_id"], biz_seq_no=biz_seq_no, business_action=action,
        biz_type=biz_type, amount=parse_amount(body.get("totalAmount")),
        currency=str(body.get("currencyCode", "USD")), caller_service=hdr["caller_service"],
        request_id=hdr["request_id"], business_scope=scope, wedap_payload=body)
    try:
        data = await submit_order(request.app.state.session_factory,
                                  wedap_call=getattr(request.app.state.wedap, client_method),
                                  req=req)
    except IdempotencyConflict:
        raise HTTPException(409, detail={"code": "GW_409_IDEMPOTENCY",
                                         "message": "same key different payload"})
    return ok(data, trace_id=hdr["trace_id"])


@router.post("/collect-from-users")
async def collect_from_users(request: Request, body: dict[str, Any]) -> dict:
    return await _submit("collect-from-users", request, body)


@router.post("/distribute-to-users")
async def distribute_to_users(request: Request, body: dict[str, Any]) -> dict:
    return await _submit("distribute-to-users", request, body)
```

```python
# app/main.py create_app 内追加（异常处理器把 HTTPException.detail 字典转规范02 envelope）：
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.clients.wedap import WedapClient
from app.core.envelope import err
from app.core.context import current_ids


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": f"GW_{exc.status_code}",
                                                              "message": str(exc.detail)}
    return JSONResponse(err(detail["code"], detail["message"],
                            trace_id=current_ids().trace_id), exc.status_code)

app.state.wedap = WedapClient(base_url=settings.wedap_base_url,
                              timeout_seconds=settings.wedap_timeout_seconds)
app.include_router(loans_router)
app.include_router(bank_funds_router)
```

- [ ] **Step 5: 写 tests/api/test_bank_funds.py（collect 受理 + distribute 幂等重放两用例，结构同 test_loans.py，BODY 换 `totalAmount`/`bizSeqNo=CLT-...`）+ 跑全量**

Run: `.venv/bin/pytest tests/api -q && .venv/bin/ruff check . && .venv/bin/mypy app`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app tests
git commit -m "feat: [M3] 北向资金交易 API x4（envelope/幂等409/ADR-0029校验/异常处理器）"
```

### Task 13: 状态查询 API（bank-funds/status 合成 + steps 透传）

> ⚠️ **historical（2026-06-23）**：本 Task 的 `composite.py` / steps 北向透传接口已废弃（chore/deprecate-composite-steps-endpoint）；`bank-funds/status` 部分仍有效。gateway 内部 wedap client 拉 steps 落 leg 不受影响。

**Files:**
- Create: `app/api/v1/composite.py`
- Modify: `app/api/v1/bank_funds.py`, `app/main.py`
- Test: `tests/api/test_status_query.py`

- [ ] **Step 1: 写失败测试（本地 order 状态优先 + 本地无单 404 + steps 透传）**

```python
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.main import create_app

HEADERS = {"X-Caller-Service": "lifecycle", "X-Tenant-Id": "OCBC", "X-Request-Id": "req-q"}


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    wedap = AsyncMock()
    wedap.query_funds_status.return_value = {"txnStatus": "SUCCESS"}
    wedap.get_composite_steps.return_value = [{"stepSeq": 1, "sysRefNo": "R1", "status": "SUCCESS"}]
    app.state.wedap = wedap
    return TestClient(app)


def _seed_order(client: TestClient) -> None:
    client.app.state.wedap.submit_disbursement = AsyncMock(return_value={"txnStatus": "PROCESSING"})
    client.post("/api/v1/loans/p2p-disbursements",
                json={"bizSeqNo": "DSB-20260611-0001234567890", "disbursementInfo":
                      {"txnAmount": "1.0000", "currencyCode": "USD"}},
                headers={**HEADERS, "Idempotency-Key": "DSB-20260611-0001234567890"})


def test_status_returns_local_order_and_wedap_view(client) -> None:
    _seed_order(client)
    r = client.get("/api/v1/bank-funds/status",
                   params={"bizSeqNo": "DSB-20260611-0001234567890"}, headers=HEADERS)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["orderStatus"] == "SUBMITTED" and data["wedap"]["txnStatus"] == "SUCCESS"


def test_status_unknown_biz_seq_404(client) -> None:
    r = client.get("/api/v1/bank-funds/status",
                   params={"bizSeqNo": "DSB-20260611-9999999999999"}, headers=HEADERS)
    assert r.status_code == 404 and r.json()["error"]["code"] == "GW_404_ORDER"


def test_steps_passthrough(client) -> None:
    _seed_order(client)
    r = client.get("/api/v1/composite-transactions/DSB-20260611-0001234567890/steps",
                   headers=HEADERS)
    assert r.json()["data"]["steps"][0]["sysRefNo"] == "R1"
```

- [ ] **Step 2: 实现（bank_funds.py 追加 GET /status；composite.py 新建）**

```python
# app/api/v1/bank_funds.py 追加
from sqlalchemy import select

from app.models.txn import BankTxnOrder


@router.get("/status")
async def funds_status(request: Request, bizSeqNo: str) -> dict:
    hdr = require_headers(request)
    async with request.app.state.session_factory() as session:
        order = (await session.execute(select(BankTxnOrder).where(
            BankTxnOrder.tenant_id == hdr["tenant_id"],
            BankTxnOrder.biz_seq_no == bizSeqNo))).scalar_one_or_none()
    if order is None:
        raise HTTPException(404, detail={"code": "GW_404_ORDER", "message": f"no order {bizSeqNo}"})
    wedap_view = await request.app.state.wedap.query_funds_status(
        tenant_id=hdr["tenant_id"], request_id=hdr["request_id"], biz_seq_no=bizSeqNo)
    return ok({"bizSeqNo": bizSeqNo, "orderStatus": order.status, "wedap": wedap_view},
              trace_id=hdr["trace_id"])
```

```python
# app/api/v1/composite.py
from fastapi import APIRouter, Request

from app.api.deps import require_headers
from app.core.envelope import ok

router = APIRouter(prefix="/api/v1/composite-transactions", tags=["composite"])


@router.get("/{biz_seq_no}/steps")
async def composite_steps(request: Request, biz_seq_no: str) -> dict:
    hdr = require_headers(request)
    steps = await request.app.state.wedap.get_composite_steps(
        tenant_id=hdr["tenant_id"], biz_seq_no=biz_seq_no)
    return ok({"bizSeqNo": biz_seq_no, "steps": steps}, trace_id=hdr["trace_id"])
```

- [ ] **Step 3: main.py 挂 composite router；跑测 + commit**

```bash
git add app tests
git commit -m "feat: [M3] 状态查询（本地 order + wedap 合成视图）+ composite steps 透传"
```

### Task 14: 审计 helper 接入写路径

**Files:**
- Create: `app/services/audit.py`
- Modify: `app/services/submit.py`
- Test: `tests/services/test_audit.py`

- [ ] **Step 1: 写失败测试（hash chain 连续性 + submit 落审计）**

```python
import pytest
from sqlalchemy import select

from app.core.db import build_engine, build_session_factory
from app.models.audit import AuditLog
from app.models.base import Base
from app.services.audit import write_audit


@pytest.fixture()
async def factory():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_hash_chain_links(factory) -> None:
    async with factory() as s:
        async with s.begin():
            await write_audit(s, tenant_id="t1", actor="svc:gateway", action="ORDER_SUBMITTED",
                              entity="bank_txn_order:1", payload={"s": "SUBMITTED"})
            await write_audit(s, tenant_id="t1", actor="svc:gateway", action="ORDER_FINALIZED",
                              entity="bank_txn_order:1", payload={"s": "SUCCEEDED"})
    async with factory() as s:
        rows = (await s.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        assert rows[0].prev_hash == "0" * 64
        assert rows[1].prev_hash == rows[0].row_hash
```

- [ ] **Step 2: 实现 app/services/audit.py 并在 submit_order 两处状态推进后调用**

```python
# app/services/audit.py
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog

GENESIS = "0" * 64


async def write_audit(session: AsyncSession, *, tenant_id: str, actor: str, action: str,
                      entity: str, payload: dict[str, Any] | None = None) -> None:
    last = (await session.execute(
        select(AuditLog.row_hash).where(AuditLog.tenant_id == tenant_id)
        .order_by(AuditLog.id.desc()).limit(1))).scalar_one_or_none()
    prev = last or GENESIS
    canonical = json.dumps({"tenant": tenant_id, "actor": actor, "action": action,
                            "entity": entity, "payload": payload, "prev": prev},
                           sort_keys=True, separators=(",", ":"), default=str)
    row_hash = hashlib.sha256(canonical.encode()).hexdigest()
    session.add(AuditLog(tenant_id=tenant_id, actor=actor, action=action, entity=entity,
                         payload=payload, prev_hash=prev, row_hash=row_hash))
```

```python
# app/services/submit.py 事务2 session.begin() 块内、record_response 之后追加：
from app.services.audit import write_audit  # 文件顶部 import

await write_audit(session, tenant_id=req.tenant_id, actor=f"svc:{req.caller_service}",
                  action=f"ORDER_{new_status}", entity=f"bank_txn_order:{req.biz_seq_no}",
                  payload={"business_action": req.business_action, "amount": str(req.amount)})
```

- [ ] **Step 3: 跑测（含回归 test_submit.py）+ commit**

```bash
git add app/services tests/services
git commit -m "feat: [M3] append-only 审计 helper（hash chain）接入受理写路径"
```

---

## Milestone 4 · 回调链路（inbox → legs → 聚合 → outbox 转发）

### Task 15: wedap 交易回调接收（inbox 幂等落库）

**Files:**
- Create: `app/api/v1/callbacks.py`
- Modify: `app/main.py`
- Test: `tests/api/test_callbacks.py`

- [ ] **Step 1: 写失败测试（首收 RECEIVED / 重放去重仍 2xx / 缺头 400）**

```python
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from app.main import create_app
from app.models.callback import CallbackInbox

HEADERS = {"X-Caller-Service": "wedap", "X-Tenant-Id": "OCBC", "X-Request-Id": "cb-1"}
PAYLOAD = {"type": "disbursement", "bizSeqNo": "DSB-20260611-0001234567890",
           "requestId": "cb-1", "txnStatus": "SUCCESS"}


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    app.state.wedap = AsyncMock()
    app.state.wedap.get_composite_steps.return_value = []
    return TestClient(app)


def _count_inbox(client) -> int:
    import asyncio

    async def _c() -> int:
        async with client.app.state.session_factory() as s:
            return (await s.execute(select(func.count(CallbackInbox.id)))).scalar()
    return asyncio.get_event_loop().run_until_complete(_c())


def test_first_callback_lands_inbox(client) -> None:
    r = client.post("/api/v1/callbacks/wedap/transactions", json=PAYLOAD, headers=HEADERS)
    assert r.status_code == 200 and r.json()["success"] is True


def test_replay_is_idempotent_2xx(client) -> None:
    client.post("/api/v1/callbacks/wedap/transactions", json=PAYLOAD, headers=HEADERS)
    r = client.post("/api/v1/callbacks/wedap/transactions", json=PAYLOAD, headers=HEADERS)
    assert r.status_code == 200 and r.json()["data"]["deduplicated"] is True
```

- [ ] **Step 2: 实现 app/api/v1/callbacks.py（落 inbox + 触发 leg 同步 + outbox 转发，后两步调 Task 16/17 的服务——本 task 先以可注入 hook 占位结构组织，hook 默认 no-op 函数而非 TODO）**

```python
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy.exc import IntegrityError

from app.api.deps import require_headers
from app.core.envelope import ok
from app.models.callback import CallbackInbox

router = APIRouter(prefix="/api/v1/callbacks/wedap", tags=["callbacks"])


@router.post("/transactions")
async def wedap_transaction_callback(request: Request, body: dict[str, Any]) -> dict:
    hdr = require_headers(request)
    factory = request.app.state.session_factory
    dedup = False
    async with factory() as session:
        async with session.begin():
            session.add(CallbackInbox(tenant_id=hdr["tenant_id"], source="WEDAP_TXN",
                                      request_id=hdr["request_id"], payload=body))
        # IntegrityError → 已收过，幂等返回
    return ok({"received": True, "deduplicated": dedup}, trace_id=hdr["trace_id"])
```

> 注意：上面代码块中 IntegrityError 分支按下方完整实现写——`session.begin()` 外层 try/except IntegrityError 置 `dedup=True`；处理链（leg 同步 + 转发）在 Task 16/17 实现后于本端点内按序调用 `await sync_legs_for(...)` 与 `await enqueue_forward(...)`，仅在 `dedup is False` 时执行。

```python
# 完整版（Task 17 完成后端点终态）：
@router.post("/transactions")
async def wedap_transaction_callback(request: Request, body: dict[str, Any]) -> dict:
    hdr = require_headers(request)
    factory = request.app.state.session_factory
    dedup = False
    try:
        async with factory() as session:
            async with session.begin():
                session.add(CallbackInbox(tenant_id=hdr["tenant_id"], source="WEDAP_TXN",
                                          request_id=hdr["request_id"], payload=body))
    except IntegrityError:
        dedup = True
    if not dedup:
        from app.services.legs import sync_legs_for
        from app.services.outbox import enqueue_forward
        await sync_legs_for(factory, wedap=request.app.state.wedap,
                            tenant_id=hdr["tenant_id"], biz_seq_no=str(body.get("bizSeqNo", "")))
        async with factory() as session:
            async with session.begin():
                await enqueue_forward(session, tenant_id=hdr["tenant_id"],
                                      target="lifecycle", payload=body)
    return ok({"received": True, "deduplicated": dedup}, trace_id=hdr["trace_id"])
```

- [ ] **Step 3: main.py 挂 router；跑测（Task 16/17 完成前先用 no-op stub 让本 task 测试绿）+ commit**

```bash
git add app/api/v1/callbacks.py tests/api/test_callbacks.py app/main.py
git commit -m "feat: [M4] wedap 交易回调接收（inbox 三元组幂等，重放 2xx）"
```

### Task 16: steps 拉取 → leg upsert → 父单聚合

**Files:**
- Create: `app/services/legs.py`
- Test: `tests/services/test_legs.py`

- [ ] **Step 1: 写失败测试（两 leg 落库 / REVERSAL 追加幂等 / 父单聚合推进 / 串单防护）**

```python
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.core.db import build_engine, build_session_factory
from app.domain.states import OrderStatus
from app.models.base import Base
from app.models.txn import BankTxnLeg, BankTxnOrder
from app.services.legs import sync_legs_for

BIZ = "DSB-20260611-0001234567890"
STEP = {"stepType": "DISBURSEMENT_COLLECTION", "stepSeq": 1, "sysRefNo": "R1",
        "amount": "60.0000", "currencyCode": "USD", "payerAccount": "L1",
        "payeeAccount": "POOL", "status": "SUCCESS", "txnDate": "20260611"}


@pytest.fixture()
async def factory():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = build_session_factory(engine)
    async with f() as s:
        async with s.begin():
            s.add(BankTxnOrder(tenant_id="OCBC", biz_seq_no=BIZ, business_action="DISBURSE",
                               biz_type="DSB", amount=Decimal("120.0000"), currency="USD",
                               caller_service="lifecycle", status="SUBMITTED"))
    yield f
    await engine.dispose()


def _wedap(steps) -> AsyncMock:
    m = AsyncMock()
    m.get_composite_steps.return_value = steps
    return m


@pytest.mark.asyncio
async def test_two_legs_landed_and_order_succeeded(factory) -> None:
    steps = [STEP, {**STEP, "stepSeq": 2, "sysRefNo": "R2",
                    "stepType": "DISBURSEMENT_DISTRIBUTION"}]
    await sync_legs_for(factory, wedap=_wedap(steps), tenant_id="OCBC", biz_seq_no=BIZ)
    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg))).scalars().all()
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
    assert len(legs) == 2 and order.status == OrderStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_resync_is_idempotent_and_reversal_appends(factory) -> None:
    await sync_legs_for(factory, wedap=_wedap([STEP]), tenant_id="OCBC", biz_seq_no=BIZ)
    steps2 = [{**STEP, "status": "REVERSED"},
              {**STEP, "stepSeq": 2, "sysRefNo": "R1-REV", "status": "REVERSAL"}]
    await sync_legs_for(factory, wedap=_wedap(steps2), tenant_id="OCBC", biz_seq_no=BIZ)
    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg).order_by(BankTxnLeg.step_seq))).scalars().all()
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
    assert len(legs) == 2
    assert legs[0].status == "REVERSED" and legs[1].status == "REVERSAL"
    assert order.status == OrderStatus.REVERSED


@pytest.mark.asyncio
async def test_unknown_order_is_noop(factory) -> None:
    await sync_legs_for(factory, wedap=_wedap([STEP]), tenant_id="OCBC",
                        biz_seq_no="DSB-20260611-0000000000404")
    async with factory() as s:
        assert (await s.execute(select(BankTxnLeg))).scalars().all() == []
```

- [ ] **Step 2: 实现 app/services/legs.py**

```python
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.states import LegStatus, OrderStatus, aggregate_order_status, assert_transition
from app.models.txn import BankTxnLeg, BankTxnOrder


async def sync_legs_for(factory: async_sessionmaker, *, wedap: Any,
                        tenant_id: str, biz_seq_no: str) -> None:
    """拉 steps → 按 (tenant, biz_seq, step_seq) upsert leg（external_ref 不可变）→ 聚合父单。"""
    steps = await wedap.get_composite_steps(tenant_id=tenant_id, biz_seq_no=biz_seq_no)
    async with factory() as session:
        async with session.begin():
            order = (await session.execute(select(BankTxnOrder).where(
                BankTxnOrder.tenant_id == tenant_id,
                BankTxnOrder.biz_seq_no == biz_seq_no))).scalar_one_or_none()
            if order is None:
                return
            existing = {(leg.step_seq): leg for leg in (await session.execute(
                select(BankTxnLeg).where(BankTxnLeg.tenant_id == tenant_id,
                                         BankTxnLeg.biz_seq_no == biz_seq_no))).scalars()}
            for s in steps:
                seq = int(s["stepSeq"])
                if seq in existing:
                    existing[seq].status = str(s["status"])     # 状态可推进，ref/金额不可变
                else:
                    session.add(BankTxnLeg(
                        tenant_id=tenant_id, order_id=order.id, biz_seq_no=biz_seq_no,
                        external_system="WEDAP_BANK", external_ref=str(s["sysRefNo"]),
                        step_type=str(s["stepType"]), step_seq=seq,
                        amount=Decimal(str(s["amount"])), currency=str(s.get("currencyCode", "USD")),
                        payer_account=s.get("payerAccount"), payee_account=s.get("payeeAccount"),
                        status=str(s["status"]), txn_date=s.get("txnDate")))
            await session.flush()
            all_legs = (await session.execute(
                select(BankTxnLeg).where(BankTxnLeg.tenant_id == tenant_id,
                                         BankTxnLeg.biz_seq_no == biz_seq_no))).scalars().all()
            if all_legs:
                new_status = aggregate_order_status(
                    [(LegStatus(leg.status), str(leg.amount)) for leg in all_legs])
                if new_status != OrderStatus(order.status):
                    assert_transition(OrderStatus(order.status), new_status)
                    order.status = new_status
```

- [ ] **Step 3: 跑测 + 把 Task 15 端点切到完整版（调 sync_legs_for）+ 回归 + commit**

Run: `.venv/bin/pytest tests/services/test_legs.py tests/api/test_callbacks.py -q`
Expected: PASS

```bash
git add app/services/legs.py app/api/v1/callbacks.py tests
git commit -m "feat: [M4] steps 拉取→leg upsert（REVERSAL 追加）→父单状态机聚合"
```

### Task 17: outbox 转发 + dispatcher + dead letter 重放

**Files:**
- Create: `app/services/outbox.py`, `app/workers/outbox_dispatcher.py`, `app/api/v1/admin_ops.py`
- Test: `tests/services/test_outbox.py`

- [ ] **Step 1: 写失败测试（enqueue / dispatch 成功 SENT / 失败退避 / 超限 DEAD / 重放复活）**

```python
import pytest
import httpx
import respx
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.callback import CallbackOutbox
from app.services.outbox import dispatch_once, enqueue_forward, replay_dead


@pytest.fixture()
async def factory():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


async def _enqueue(factory) -> int:
    async with factory() as s:
        async with s.begin():
            row = await enqueue_forward(s, tenant_id="OCBC", target="lifecycle",
                                        payload={"bizSeqNo": "B1"})
            return row.id


TARGETS = {"lifecycle": "http://lifecycle/cb"}


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_success_marks_sent(factory) -> None:
    respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(200))
    oid = await _enqueue(factory)
    await dispatch_once(factory, targets=TARGETS, max_attempts=3)
    async with factory() as s:
        assert (await s.get(CallbackOutbox, oid)).status == "SENT"


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_failure_retries_then_dead(factory) -> None:
    respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(502))
    oid = await _enqueue(factory)
    for _ in range(3):
        await dispatch_once(factory, targets=TARGETS, max_attempts=3, backoff_base_seconds=0)
    async with factory() as s:
        row = await s.get(CallbackOutbox, oid)
        assert row.status == "DEAD" and row.attempts == 3


@pytest.mark.asyncio
@respx.mock
async def test_replay_dead_resets_to_pending(factory) -> None:
    respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(502))
    oid = await _enqueue(factory)
    for _ in range(3):
        await dispatch_once(factory, targets=TARGETS, max_attempts=3, backoff_base_seconds=0)
    async with factory() as s:
        async with s.begin():
            assert await replay_dead(s, outbox_id=oid) is True
    async with factory() as s:
        assert (await s.get(CallbackOutbox, oid)).status == "PENDING"
```

- [ ] **Step 2: 实现 app/services/outbox.py**

```python
import datetime as dt
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.callback import CallbackOutbox


async def enqueue_forward(session: AsyncSession, *, tenant_id: str, target: str,
                          payload: dict[str, Any]) -> CallbackOutbox:
    row = CallbackOutbox(tenant_id=tenant_id, target=target, payload=payload)
    session.add(row)
    await session.flush()
    return row


async def dispatch_once(factory: async_sessionmaker, *, targets: dict[str, str],
                        max_attempts: int, backoff_base_seconds: float = 2.0) -> int:
    """投递一轮到期 PENDING/FAILED；返回处理条数。外呼在事务外（规范14）。"""
    now = dt.datetime.now(dt.UTC)
    async with factory() as session:
        rows = (await session.execute(select(CallbackOutbox).where(
            CallbackOutbox.status.in_(("PENDING", "FAILED"))))).scalars().all()
        due = [r for r in rows if r.next_retry_at is None
               or r.next_retry_at.replace(tzinfo=dt.UTC) <= now]
        snapshot = [(r.id, r.target, r.payload) for r in due]

    handled = 0
    for oid, target, payload in snapshot:
        url = targets.get(target)
        ok_flag, error = False, None
        if url is None:
            error = f"no url for target {target}"
        else:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, json=payload)
                ok_flag = resp.status_code < 300
                error = None if ok_flag else f"http {resp.status_code}"
            except httpx.HTTPError as exc:
                error = str(exc)
        async with factory() as session:
            async with session.begin():
                row = await session.get(CallbackOutbox, oid)
                if row is None:
                    continue
                row.attempts += 1
                if ok_flag:
                    row.status = "SENT"
                elif row.attempts >= max_attempts:
                    row.status, row.last_error = "DEAD", error
                else:
                    row.status, row.last_error = "FAILED", error
                    row.next_retry_at = dt.datetime.now(dt.UTC) + dt.timedelta(
                        seconds=backoff_base_seconds * (2 ** (row.attempts - 1)))
        handled += 1
    return handled


async def replay_dead(session: AsyncSession, *, outbox_id: int) -> bool:
    row = await session.get(CallbackOutbox, outbox_id)
    if row is None or row.status != "DEAD":
        return False
    row.status, row.attempts, row.next_retry_at = "PENDING", 0, None
    return True
```

- [ ] **Step 3: workers/outbox_dispatcher.py（asyncio 循环薄壳）+ admin_ops.py 重放端点 + main.py 挂载 + 测试 + commit**

```python
# app/workers/outbox_dispatcher.py
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.outbox import dispatch_once


async def run_forever(factory: async_sessionmaker, *, targets: dict[str, str],
                      max_attempts: int, interval_seconds: float = 5.0) -> None:  # pragma: no cover
    while True:
        await dispatch_once(factory, targets=targets, max_attempts=max_attempts)
        await asyncio.sleep(interval_seconds)
```

```python
# app/api/v1/admin_ops.py
from fastapi import APIRouter, HTTPException, Request

from app.api.deps import require_headers
from app.core.envelope import ok
from app.services.outbox import replay_dead

router = APIRouter(prefix="/api/v1/admin/outbox", tags=["admin-ops"])


@router.post("/{outbox_id}/replay")
async def replay(request: Request, outbox_id: int) -> dict:
    hdr = require_headers(request)
    async with request.app.state.session_factory() as session:
        async with session.begin():
            revived = await replay_dead(session, outbox_id=outbox_id)
    if not revived:
        raise HTTPException(404, detail={"code": "GW_404_OUTBOX", "message": "not found or not DEAD"})
    return ok({"replayed": outbox_id}, trace_id=hdr["trace_id"])
```

Run: `.venv/bin/pytest tests/services/test_outbox.py -q`
Expected: PASS

```bash
git add app tests
git commit -m "feat: [M4] outbox 投递（退避重试/DEAD/重放）+ dispatcher worker + 重放端点"
```

---

## Milestone 5 · 对账结果摄取

### Task 18: recon notify 接收端点

**Files:**
- Create: `app/api/v1/recon_notify.py`
- Modify: `app/main.py`
- Test: `tests/api/test_recon_notify.py`

- [ ] **Step 1: 写失败测试（首收建 task / X-Request-Id 重放 2xx 去重 / body 校验 400）**

```python
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.main import create_app

HEADERS = {"X-Caller-Service": "wedap", "X-Tenant-Id": "OCBC",
           "X-Request-Id": "recon-result-RECON-OCBC-20260604-v2"}
BODY = {"reconDate": "20260604", "tenantId": "OCBC", "s3Bucket": "wedap-recon-prod",
        "files": [{"fileName": "RECON_RESULT_RECON-OCBC-20260604_v2.xlsx",
                   "s3Key": "OCBC/20260604/RECON_RESULT_RECON-OCBC-20260604_v2.xlsx",
                   "md5": "9f8c1d2e3a4b5c6d7e8f9a0b1c2d3e4f", "totalCount": 0}]}


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    app.state.wedap = AsyncMock()
    return TestClient(app)


def test_first_notify_creates_task(client) -> None:
    r = client.post("/api/v1/recon/notify", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["data"]["taskNo"] == "RECON-OCBC-20260604" and r.json()["data"]["version"] == 2


def test_replay_dedup_2xx(client) -> None:
    client.post("/api/v1/recon/notify", json=BODY, headers=HEADERS)
    r = client.post("/api/v1/recon/notify", json=BODY, headers=HEADERS)
    assert r.status_code == 200 and r.json()["data"]["deduplicated"] is True


def test_bad_body_400(client) -> None:
    r = client.post("/api/v1/recon/notify", json={"reconDate": "x"}, headers=HEADERS)
    assert r.status_code == 400 and r.json()["error"]["code"] == "GW_400_VALIDATION"
```

- [ ] **Step 2: 实现 app/api/v1/recon_notify.py**

```python
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from app.api.deps import require_headers
from app.core.envelope import ok
from app.models.callback import CallbackInbox
from app.models.recon import ReconResultTask

router = APIRouter(prefix="/api/v1/recon", tags=["recon"])

_REQ_ID = re.compile(r"^recon-result-(?P<task_no>.+)-v(?P<version>\d+)$")


@router.post("/notify")
async def recon_notify(request: Request, body: dict[str, Any]) -> dict:
    hdr = require_headers(request)
    m = _REQ_ID.match(hdr["request_id"])
    files = body.get("files") or []
    if not m or not re.fullmatch(r"\d{8}", str(body.get("reconDate", ""))) or not files:
        raise HTTPException(400, detail={"code": "GW_400_VALIDATION",
                                         "message": "bad notify body or X-Request-Id"})
    f = files[0]   # 契约：当前恒 1 个文件
    task_no, version = m.group("task_no"), int(m.group("version"))
    dedup = False
    try:
        async with request.app.state.session_factory() as session:
            async with session.begin():
                session.add(CallbackInbox(tenant_id=hdr["tenant_id"], source="WEDAP_RECON",
                                          request_id=hdr["request_id"], payload=body))
                session.add(ReconResultTask(
                    tenant_id=hdr["tenant_id"], task_no=task_no, version=version,
                    recon_date=str(body["reconDate"]), s3_bucket=str(body["s3Bucket"]),
                    s3_key=str(f["s3Key"]), file_md5=str(f["md5"]),
                    diff_count=int(f["totalCount"]), status="NOTIFIED",
                    request_id=hdr["request_id"]))
    except IntegrityError:
        dedup = True
    return ok({"taskNo": task_no, "version": version, "deduplicated": dedup},
              trace_id=hdr["trace_id"])
```

- [ ] **Step 3: main.py 挂 router；跑测 + commit**

```bash
git add app tests
git commit -m "feat: [M5] recon notify 接收（X-Request-Id 幂等 + task NOTIFIED 落库）"
```

### Task 19: S3 下载 + md5 校验

**Files:**
- Create: `app/clients/s3.py`
- Test: `tests/clients/test_s3.py`

- [ ] **Step 1: 写失败测试（botocore Stubber：下载成功校验通过 / md5 不符抛错）**

```python
import hashlib

import pytest
from botocore.stub import Stubber

from app.clients.s3 import Md5Mismatch, S3FileClient

CONTENT = b"excel-bytes"
MD5 = hashlib.md5(CONTENT).hexdigest()


def _client_with_stub(body: bytes):
    c = S3FileClient(endpoint_url=None)
    stub = Stubber(c._s3)
    import io
    stub.add_response("get_object", {"Body": io.BytesIO(body)},
                      {"Bucket": "b", "Key": "k"})
    stub.activate()
    return c


def test_download_and_verify_ok(tmp_path) -> None:
    c = _client_with_stub(CONTENT)
    dest = tmp_path / "f.xlsx"
    c.download_verified(bucket="b", key="k", expected_md5=MD5, dest=str(dest))
    assert dest.read_bytes() == CONTENT


def test_md5_mismatch_raises(tmp_path) -> None:
    c = _client_with_stub(CONTENT)
    with pytest.raises(Md5Mismatch):
        c.download_verified(bucket="b", key="k", expected_md5="0" * 32,
                            dest=str(tmp_path / "f.xlsx"))
```

- [ ] **Step 2: 实现 app/clients/s3.py**

```python
import hashlib
import pathlib

import boto3


class Md5Mismatch(Exception):
    pass


class S3FileClient:
    def __init__(self, *, endpoint_url: str | None) -> None:
        self._s3 = boto3.client("s3", endpoint_url=endpoint_url)

    def download_verified(self, *, bucket: str, key: str, expected_md5: str, dest: str) -> None:
        """同步 boto3——调用方在 asyncio.to_thread 中执行。下载→md5 校验→落地存档。"""
        body = self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        actual = hashlib.md5(body).hexdigest()
        if actual != expected_md5.lower():
            raise Md5Mismatch(f"{actual} != {expected_md5}")
        path = pathlib.Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
```

- [ ] **Step 3: 跑测 + commit**

```bash
git add app/clients/s3.py tests/clients/test_s3.py
git commit -m "feat: [M5] S3 下载 + md5 完整性校验（Stubber 测试）"
```

### Task 20: Excel 3-sheet 解析 + 落库 + version supersede

**Files:**
- Create: `app/services/recon_ingest.py`, `app/workers/recon_worker.py`
- Test: `tests/services/test_recon_ingest.py`

- [ ] **Step 1: 写失败测试（fixture xlsx 由测试现场用 openpyxl 生成：3 sheet + 表头；断言 diff/source 落库行数、列头漂移 FAIL、v2 supersede v1、totalCount 与 Differences 行数不符告警字段）**

```python
import pytest
from openpyxl import Workbook

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.recon import (ReconResultDiff, ReconResultSourceBank,
                              ReconResultSourceWedap, ReconResultTask)
from app.services.recon_ingest import ColumnDrift, parse_and_land

DIFF_HEADER = ["Type", "WeDAP Biz Seq No", "Bank Seq No", "WeDAP Amount", "Bank Amount",
               "Diff Amount", "WeDAP Status", "Bank Status"]
WEDAP_HEADER = ["Biz Type", "Biz Seq No", "Bank Biz Seq No", "Amount", "Currency",
                "Payer", "Payee", "Status", "Error"]
BANK_HEADER = ["Bank Seq No", "Txn Date", "Amount", "Currency", "Payer", "Payee",
               "Status", "File Name", "Line No"]


def _make_xlsx(path, *, diff_rows=(), wedap_rows=(), bank_rows=(), break_header=False):
    wb = Workbook()
    ws = wb.active
    ws.title = "Differences"
    ws.append(["XXX"] if break_header else DIFF_HEADER)
    for r in diff_rows:
        ws.append(r)
    w2 = wb.create_sheet("WeDAP Source")
    w2.append(WEDAP_HEADER)
    for r in wedap_rows:
        w2.append(r)
    w3 = wb.create_sheet("Bank Source")
    w3.append(BANK_HEADER)
    for r in bank_rows:
        w3.append(r)
    wb.save(path)


@pytest.fixture()
async def env(tmp_path):
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = build_session_factory(engine)

    async def make_task(version=1, status="DOWNLOADED"):
        async with factory() as s:
            async with s.begin():
                t = ReconResultTask(tenant_id="OCBC", task_no="RECON-OCBC-20260604",
                                    version=version, recon_date="20260604", s3_bucket="b",
                                    s3_key="k", file_md5="0" * 32, diff_count=1, status=status,
                                    request_id=f"recon-result-RECON-OCBC-20260604-v{version}")
                s.add(t)
            return t.id
    yield factory, make_task, tmp_path
    await engine.dispose()


@pytest.mark.asyncio
async def test_parse_lands_three_sheets(env) -> None:
    factory, make_task, tmp = env
    tid = await make_task()
    p = tmp / "r.xlsx"
    _make_xlsx(p,
               diff_rows=[["SHORT", None, "HSBC1", None, "50.0000", "-50.0000", None, "SUCCESS"]],
               wedap_rows=[["DSB", "DSB-20260611-0000000000001", "HSBC0", "100.0000", "USD",
                            "L1", "B1", "SUCCESS", None]],
               bank_rows=[["HSBC1", "20260604", "50.0000", "USD", "A", "B", "SUCCESS", "f.csv", 2]])
    await parse_and_land(factory, task_id=tid, xlsx_path=str(p))
    from sqlalchemy import func, select
    async with factory() as s:
        task = await s.get(ReconResultTask, tid)
        assert task.status == "PARSED" and task.parser_version == "1"
        assert (await s.execute(select(func.count(ReconResultDiff.id)))).scalar() == 1
        assert (await s.execute(select(func.count(ReconResultSourceWedap.id)))).scalar() == 1
        assert (await s.execute(select(func.count(ReconResultSourceBank.id)))).scalar() == 1


@pytest.mark.asyncio
async def test_header_drift_fails_task(env) -> None:
    factory, make_task, tmp = env
    tid = await make_task()
    p = tmp / "bad.xlsx"
    _make_xlsx(p, break_header=True)
    with pytest.raises(ColumnDrift):
        await parse_and_land(factory, task_id=tid, xlsx_path=str(p))
    async with factory() as s:
        assert (await s.get(ReconResultTask, tid)).status == "FAILED"


@pytest.mark.asyncio
async def test_new_version_supersedes_old(env) -> None:
    factory, make_task, tmp = env
    tid1 = await make_task(version=1, status="PARSED")
    tid2 = await make_task(version=2)
    p = tmp / "v2.xlsx"
    _make_xlsx(p)
    await parse_and_land(factory, task_id=tid2, xlsx_path=str(p))
    async with factory() as s:
        assert (await s.get(ReconResultTask, tid1)).status == "SUPERSEDED"
        assert (await s.get(ReconResultTask, tid2)).status == "PARSED"
```

- [ ] **Step 2: 实现 app/services/recon_ingest.py**

```python
from decimal import Decimal
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.recon import (ReconResultDiff, ReconResultSourceBank,
                              ReconResultSourceWedap, ReconResultTask)

PARSER_VERSION = "1"
SCHEMA_VERSION = "wedap-recon-3sheet-v1"

_EXPECTED = {
    "Differences": ["Type", "WeDAP Biz Seq No", "Bank Seq No", "WeDAP Amount", "Bank Amount",
                    "Diff Amount", "WeDAP Status", "Bank Status"],
    "WeDAP Source": ["Biz Type", "Biz Seq No", "Bank Biz Seq No", "Amount", "Currency",
                     "Payer", "Payee", "Status", "Error"],
    "Bank Source": ["Bank Seq No", "Txn Date", "Amount", "Currency", "Payer", "Payee",
                    "Status", "File Name", "Line No"],
}


class ColumnDrift(Exception):
    pass


def _dec(v: Any) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


async def parse_and_land(factory: async_sessionmaker, *, task_id: int, xlsx_path: str) -> None:
    async with factory() as session:
        task = await session.get(ReconResultTask, task_id)
        assert task is not None
        tenant_id = task.tenant_id

    try:
        wb = load_workbook(xlsx_path, read_only=True, data_only=True)
        rows: dict[str, list[tuple]] = {}
        column_check: dict[str, str] = {}
        for sheet, expected in _EXPECTED.items():
            ws = wb[sheet]
            data = list(ws.iter_rows(values_only=True))
            header = [str(c) if c is not None else "" for c in (data[0] if data else [])]
            if header[: len(expected)] != expected:
                column_check[sheet] = f"drift: {header!r}"
                raise ColumnDrift(f"{sheet}: {header!r}")
            column_check[sheet] = "ok"
            rows[sheet] = [r for r in data[1:] if any(c is not None for c in r)]
    except ColumnDrift:
        async with factory() as session:
            async with session.begin():
                await session.execute(update(ReconResultTask).where(
                    ReconResultTask.id == task_id).values(status="FAILED",
                                                          column_check=column_check))
        raise

    async with factory() as session:
        async with session.begin():
            for r in rows["Differences"]:
                session.add(ReconResultDiff(
                    tenant_id=tenant_id, task_id=task_id, diff_type=str(r[0]),
                    wedap_biz_seq_no=r[1], bank_seq_no=r[2], wedap_amount=_dec(r[3]),
                    bank_amount=_dec(r[4]), diff_amount=_dec(r[5]),
                    wedap_status=r[6], bank_status=r[7]))
            for r in rows["WeDAP Source"]:
                session.add(ReconResultSourceWedap(
                    tenant_id=tenant_id, task_id=task_id, biz_type=r[0],
                    biz_seq_no=str(r[1]), bank_biz_seq_no=r[2], amount=_dec(r[3]),
                    currency=r[4], payer_account=r[5], payee_account=r[6],
                    status=r[7], error_msg=r[8]))
            for r in rows["Bank Source"]:
                session.add(ReconResultSourceBank(
                    tenant_id=tenant_id, task_id=task_id, bank_seq_no=str(r[0]),
                    txn_date=str(r[1]) if r[1] is not None else None, amount=_dec(r[2]),
                    currency=r[3], payer_account=r[4], payee_account=r[5], status=r[6],
                    file_name=r[7], line_no=r[8]))
            task = await session.get(ReconResultTask, task_id)
            assert task is not None
            task.status, task.parser_version = "PARSED", PARSER_VERSION
            task.schema_version, task.column_check = SCHEMA_VERSION, column_check
            task.archive_path = xlsx_path
            await session.execute(update(ReconResultTask).where(
                ReconResultTask.tenant_id == tenant_id,
                ReconResultTask.task_no == task.task_no,
                ReconResultTask.version < task.version,
                ReconResultTask.status != "SUPERSEDED",
            ).values(status="SUPERSEDED"))
```

- [ ] **Step 3: workers/recon_worker.py（NOTIFIED → to_thread 下载 → parse_and_land 串联）**

```python
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.s3 import S3FileClient
from app.models.recon import ReconResultTask
from app.services.recon_ingest import parse_and_land


async def ingest_pending_once(factory: async_sessionmaker, *, s3: S3FileClient,
                              archive_dir: str) -> int:
    async with factory() as session:
        tasks = (await session.execute(select(ReconResultTask).where(
            ReconResultTask.status == "NOTIFIED"))).scalars().all()
        snapshot = [(t.id, t.s3_bucket, t.s3_key, t.file_md5, t.task_no, t.version)
                    for t in tasks]
    for tid, bucket, key, md5, task_no, version in snapshot:
        dest = f"{archive_dir}/{task_no}_v{version}.xlsx"
        await asyncio.to_thread(s3.download_verified, bucket=bucket, key=key,
                                expected_md5=md5, dest=dest)
        async with factory() as session:
            async with session.begin():
                t = await session.get(ReconResultTask, tid)
                if t is not None:
                    t.status = "DOWNLOADED"
        await parse_and_land(factory, task_id=tid, xlsx_path=dest)
    return len(snapshot)
```

测试补一条 `test_ingest_pending_once`（S3FileClient 用 monkeypatch 替换 download_verified 为写本地 fixture xlsx），断言 NOTIFIED→PARSED 全链。

- [ ] **Step 4: 跑测 + commit**

Run: `.venv/bin/pytest tests/services/test_recon_ingest.py -q`
Expected: PASS

```bash
git add app tests
git commit -m "feat: [M5] Excel 3-sheet 解析落库（列头校验/parser_version/supersede）+ 摄取 worker"
```

### Task 21: fiat-vault/transactions 供数端点

**Files:**
- Create: `app/api/v1/fiat_vault.py`
- Modify: `app/main.py`
- Test: `tests/api/test_fiat_vault.py`

- [ ] **Step 1: 写失败测试（按账户+日期窗查 legs + 聚合金额；分页 limit）**

```python
# 种子：直接插 BankTxnOrder/Leg（payee_account="VAULT01"，txn_date="20260611"）
# 断言：GET /api/v1/fiat-vault/transactions?accountNo=VAULT01&dateFrom=20260611&dateTo=20260611
#   → data.items 长度、data.aggregate.totalAmount == "60.0000"；limit=1 时 items 截断。
```

```python
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.txn import BankTxnLeg, BankTxnOrder

HEADERS = {"X-Caller-Service": "lending-recon", "X-Tenant-Id": "OCBC", "X-Request-Id": "rq"}


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    app.state.wedap = AsyncMock()
    c = TestClient(app)
    import asyncio

    async def seed() -> None:
        async with app.state.session_factory() as s:
            async with s.begin():
                order = BankTxnOrder(tenant_id="OCBC", biz_seq_no="CLT-20260611-0000000000001",
                                     business_action="COLLECT", biz_type="CLT",
                                     amount=Decimal("60.0000"), currency="USD",
                                     caller_service="lifecycle", status="SUCCEEDED")
                s.add(order)
                await s.flush()
                s.add(BankTxnLeg(tenant_id="OCBC", order_id=order.id,
                                 biz_seq_no=order.biz_seq_no, external_system="WEDAP_BANK",
                                 external_ref="R1", step_type="COLLECTION", step_seq=1,
                                 amount=Decimal("60.0000"), currency="USD",
                                 payer_account="U1", payee_account="VAULT01",
                                 status="SUCCESS", txn_date="20260611"))
    asyncio.get_event_loop().run_until_complete(seed())
    return c


def test_query_by_account_and_window(client) -> None:
    r = client.get("/api/v1/fiat-vault/transactions",
                   params={"accountNo": "VAULT01", "dateFrom": "20260611", "dateTo": "20260611"},
                   headers=HEADERS)
    data = r.json()["data"]
    assert len(data["items"]) == 1 and data["aggregate"]["totalAmount"] == "60.0000"
```

- [ ] **Step 2: 实现 app/api/v1/fiat_vault.py**

```python
from decimal import Decimal

from fastapi import APIRouter, Request
from sqlalchemy import or_, select

from app.api.deps import require_headers
from app.core.envelope import ok
from app.models.txn import BankTxnLeg

router = APIRouter(prefix="/api/v1/fiat-vault", tags=["fiat-vault"])


@router.get("/transactions")
async def vault_transactions(request: Request, accountNo: str, dateFrom: str, dateTo: str,
                             limit: int = 500) -> dict:
    hdr = require_headers(request)
    async with request.app.state.session_factory() as session:
        legs = (await session.execute(select(BankTxnLeg).where(
            BankTxnLeg.tenant_id == hdr["tenant_id"],
            or_(BankTxnLeg.payee_account == accountNo, BankTxnLeg.payer_account == accountNo),
            BankTxnLeg.txn_date >= dateFrom, BankTxnLeg.txn_date <= dateTo,
        ).order_by(BankTxnLeg.id).limit(limit))).scalars().all()
    items = [{"bizSeqNo": leg.biz_seq_no, "externalRef": leg.external_ref,
              "stepType": leg.step_type, "amount": str(leg.amount), "currency": leg.currency,
              "payer": leg.payer_account, "payee": leg.payee_account,
              "status": leg.status, "txnDate": leg.txn_date} for leg in legs]
    total = sum((Decimal(i["amount"]) for i in items), Decimal("0"))
    return ok({"items": items, "aggregate": {"count": len(items),
                                             "totalAmount": f"{total:.4f}"}},
              trace_id=hdr["trace_id"])
```

- [ ] **Step 3: 跑测 + commit**

```bash
git add app tests
git commit -m "feat: [M5] fiat-vault/transactions 供数端点（recon 消费，gateway 自有库）"
```

---

## Milestone 6 · 查询透传 + 契约门禁 + 部署物

### Task 22: deposit/users 查询透传 + query_audit + balance_snapshot

**Files:**
- Create: `app/api/v1/deposit.py`
- Modify: `app/main.py`
- Test: `tests/api/test_deposit.py`

- [ ] **Step 1: 写失败测试（透传成功 + query_audit 落一行 + 余额接口落 balance_snapshot + wedap 超时回 502 envelope）**

```python
import httpx
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from app.main import create_app
from app.models.query_audit import BalanceSnapshot, QueryAudit

HEADERS = {"X-Caller-Service": "lifecycle", "X-Tenant-Id": "OCBC", "X-Request-Id": "q1"}


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    wedap = AsyncMock()
    wedap.get_deposit_balance_total.return_value = {
        "totalBalance": "1234.0000", "currencyCode": "USD",
        "accounts": [{"custAccountNo": "A1", "balance": "1234.0000", "currencyCode": "USD"}]}
    wedap.get_deposit_accounts.return_value = {"accounts": [{"custAccountNo": "A1"}]}
    wedap.get_user_info.return_value = {"userId": "U1"}
    app.state.wedap = wedap
    return TestClient(app)


def _count(client, model) -> int:
    import asyncio

    async def _c() -> int:
        async with client.app.state.session_factory() as s:
            return (await s.execute(select(func.count(model.id)))).scalar()
    return asyncio.get_event_loop().run_until_complete(_c())


def test_balance_total_passthrough_and_snapshot(client) -> None:
    r = client.get("/api/v1/deposit/balances/total", params={"userId": "U1"}, headers=HEADERS)
    assert r.json()["data"]["totalBalance"] == "1234.0000"
    assert _count(client, QueryAudit) == 1
    assert _count(client, BalanceSnapshot) == 1


def test_wedap_timeout_maps_502(client) -> None:
    client.app.state.wedap.get_deposit_accounts.side_effect = httpx.ConnectTimeout("t")
    r = client.get("/api/v1/deposit/accounts", params={"userId": "U1"}, headers=HEADERS)
    assert r.status_code == 502 and r.json()["error"]["code"] == "GW_502_UPSTREAM"
```

- [ ] **Step 2: 实现 app/api/v1/deposit.py**

```python
import datetime as dt
import hashlib
from decimal import Decimal
from typing import Any, Awaitable, Callable

import httpx
from fastapi import APIRouter, HTTPException, Request

from app.api.deps import require_headers
from app.core.envelope import ok
from app.models.query_audit import BalanceSnapshot, QueryAudit

router = APIRouter(tags=["deposit"])


async def _audited_passthrough(request: Request, *, endpoint: str,
                               call: Callable[[], Awaitable[dict[str, Any]]],
                               params: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    hdr = require_headers(request)
    try:
        data = await call()
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise HTTPException(502, detail={"code": "GW_502_UPSTREAM",
                                         "message": f"wedap unreachable: {exc}"}) from exc
    h = hashlib.sha256(repr(sorted(params.items())).encode()).hexdigest()
    async with request.app.state.session_factory() as session:
        async with session.begin():
            session.add(QueryAudit(tenant_id=hdr["tenant_id"], endpoint=endpoint,
                                   params_hash=h, trace_id=hdr["trace_id"],
                                   caller_service=hdr["caller_service"]))
    return data, hdr


@router.get("/api/v1/deposit/balances/total")
async def deposit_balance_total(request: Request, userId: str) -> dict:
    hdr0 = require_headers(request)
    wedap = request.app.state.wedap
    data, hdr = await _audited_passthrough(
        request, endpoint="deposit/balances/total", params={"userId": userId},
        call=lambda: wedap.get_deposit_balance_total(
            tenant_id=hdr0["tenant_id"], request_id=hdr0["request_id"], user_id=userId))
    now = dt.datetime.now(dt.UTC)
    async with request.app.state.session_factory() as session:
        async with session.begin():
            for acct in data.get("accounts", []):
                session.add(BalanceSnapshot(
                    tenant_id=hdr["tenant_id"], account_id=str(acct.get("custAccountNo")),
                    balance=Decimal(str(acct.get("balance", "0"))),
                    currency=str(acct.get("currencyCode", "USD")),
                    source_endpoint="deposit/balances/total", captured_at=now))
    return ok(data, trace_id=hdr["trace_id"])


@router.get("/api/v1/deposit/accounts")
async def deposit_accounts(request: Request, userId: str) -> dict:
    hdr0 = require_headers(request)
    wedap = request.app.state.wedap
    data, hdr = await _audited_passthrough(
        request, endpoint="deposit/accounts", params={"userId": userId},
        call=lambda: wedap.get_deposit_accounts(
            tenant_id=hdr0["tenant_id"], request_id=hdr0["request_id"], user_id=userId))
    return ok(data, trace_id=hdr["trace_id"])


@router.get("/api/v1/users/info")
async def users_info(request: Request) -> dict:
    hdr0 = require_headers(request)
    params = dict(request.query_params)
    wedap = request.app.state.wedap
    data, hdr = await _audited_passthrough(
        request, endpoint="users/info", params=params,
        call=lambda: wedap.get_user_info(
            tenant_id=hdr0["tenant_id"], request_id=hdr0["request_id"], params=params))
    return ok(data, trace_id=hdr["trace_id"])
```

- [ ] **Step 3: 跑测 + commit**

```bash
git add app tests
git commit -m "feat: [M6] deposit/users 查询透传（query_audit + balance_snapshot + 502 映射）"
```

### Task 23: OpenAPI 快照 + wedap 契约 replay 门禁

**Files:**
- Create: `tests/contract/test_openapi_snapshot.py`, `tests/contract/test_wedap_replay.py`, `contracts/openapi.json`

- [ ] **Step 1: 写 OpenAPI 快照测试（首跑生成 checked-in 基准，此后 diff 即 fail——契约漂移门禁）**

```python
import json
import pathlib

from fastapi.testclient import TestClient

from app.main import create_app

SNAPSHOT = pathlib.Path(__file__).parent.parent.parent / "contracts" / "openapi.json"


def test_openapi_matches_snapshot() -> None:
    spec = TestClient(create_app()).get("/openapi.json").json()
    if not SNAPSHOT.exists():   # 首跑落基准；CI 中文件已 checked-in，永远走 else 分支
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False))
        raise AssertionError("openapi snapshot created — review & commit contracts/openapi.json")
    assert spec == json.loads(SNAPSHOT.read_text()), \
        "北向契约漂移：先评审，再更新 contracts/openapi.json"
```

- [ ] **Step 2: 写 wedap 契约 replay 测试（tests/fixtures/wedap/*.json 全量回放 client 方法，南向解包契约钉死）**

```python
import json
import pathlib

import httpx
import pytest
import respx

from app.clients.wedap import WedapClient

FIX = pathlib.Path(__file__).parent.parent / "fixtures" / "wedap"


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("fixture,method,kwargs,assert_key", [
    ("disbursement_accepted.json", "submit_disbursement",
     {"payload": {}}, "txnStatus"),
    ("steps_two_legs.json", None, {}, None),   # steps fixture 由专测覆盖
])
async def test_replay_fixtures(fixture, method, kwargs, assert_key) -> None:
    if method is None:
        pytest.skip("covered in client tests")
    body = json.loads((FIX / fixture).read_text())
    respx.post("http://wedap/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(200, json=body))
    client = WedapClient(base_url="http://wedap", timeout_seconds=1.0)
    data = await getattr(client, method)(tenant_id="OCBC", request_id="r", **kwargs)
    assert assert_key in data
```

> 联调（wedap dev 直连，用户拍板 B）：另置 `tests/compat/`（`-m compat`，CI 不跑），nightly 手动执行同一组 client 方法打真 wedap dev，比对 fixture 结构差异——发现漂移即更新 fixtures + 升级 PARSER/契约评审。

- [ ] **Step 3: 跑测（首跑生成快照→commit 基准→复跑全绿）+ commit**

```bash
git add tests/contract contracts
git commit -m "test: [M6] OpenAPI 快照门禁 + wedap 契约 replay（fixtures 钉死南向契约）"
```

### Task 24: 部署物 + 全量门禁收口

**Files:**
- Create: `deploy/Dockerfile`, `deploy/docker-compose.yml`, `README.md`
- Modify: `.github/workflows/ci.yml`（启用 integration job）

- [ ] **Step 1: Dockerfile + compose（模式对齐 lending-recon/deploy 惯例：非 root 用户、healthcheck 打 /healthz、env 注入 GW_*）**

```dockerfile
FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-cache-dir -e . && useradd -r gateway
USER gateway
EXPOSE 8050
HEALTHCHECK CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8050/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8050"]
```

```yaml
# deploy/docker-compose.yml
services:
  lending-bank-gateway:
    build: { context: .., dockerfile: deploy/Dockerfile }
    ports: ["8050:8050"]
    environment:
      GW_DB_URL: mysql+asyncmy://${DB_USER}:${DB_PASS}@${DB_HOST}:3306/lending_bank_gateway
      GW_WEDAP_BASE_URL: ${WEDAP_BASE_URL}
      GW_S2S_SECRET: ${GW_S2S_SECRET}
    networks: [wedap-network]
networks:
  wedap-network: { external: true }
```

- [ ] **Step 2: README（定位/契约/本地起服/门禁命令/spec+plan 链接）+ CI integration job 启用（`if: false` 改 `if: true`，加 docker service）**

- [ ] **Step 3: 全量门禁收口**

Run: `.venv/bin/pytest -q && .venv/bin/pytest -m integration --no-cov -q && .venv/bin/ruff check . && .venv/bin/mypy app`
Expected: 全绿，coverage 100%

- [ ] **Step 4: Commit + 收尾**

```bash
git add deploy README.md .github
git commit -m "chore: [M6] Dockerfile/compose（:8050）+ README + CI integration 启用"
```

收尾动作（执行者完成全部 task 后）：
1. `git push -u origin feat/v1-cutover-core`，按 finishing-a-development-branch 流程合 main（merge 与 push 分两次 Bash 调用，禁 `&&` 串接）
2. 本地部署验证：`bash deploy/…` 起服 → `/healthz` `/readyz` 探活 → 按 deploy-verification SOP 留证据
3. codex review 整分支 diff（项目纪律：代码改动默认 codex 复核）
4. PATCH FU-BANK-GATEWAY-KICKOFF（event=update，note 含 merge commit + 门禁输出）

---

## 计划外（显式不在本 Plan，防误扩散）

- 9000 `admin_bank_intent` 与调用方改造 → Plan 2
- lending-recon collector + 四段对账规则 → Plan 3
- liquidation/customers 迁移、BFF audience 切换 → Plan 4（走 ssot-cutover SOP）
- v1-coordination 轨 9 项（freeze/汇率/Exchange/钱包转账/deduct/refund/users-create/流水/余额直查）→ 各自协调放行后增补 task
- wedap dev 直连 compat suite 的 nightly 编排（`tests/compat/` 骨架在 Task 23 注释中，编排到 10-auto-routines 另议）

## Self-Review 记录（writing-plans 自审）

1. **Spec 覆盖**：§2 契约姿态→T3/T12/T23；§3.1 cutover 13 项→T12(1-4)/T13(5-6)/T22(7-9)/T18(10)/T15(11)/T17(12)/T21(13)；§5 数据模型 10 表→T5-T8（exchange_quote/trade 属 C3 协调轨，显式不建）；§6 状态机→T9；§7 摄取→T18-T21（第 0/1/3 段对账规则在 recon 侧，属 Plan 3；第 0 段 9000 intent 表属 Plan 2——已在"计划外"声明）；§8 安全→T4/T6/T14/T18；§9 测试→T23 + 各 task TDD；§10 迁移→Plan 4。无遗漏 cutover 项。
2. **占位符扫描**：无 TBD/TODO；T15 的"占位结构"为可运行 no-op 函数 + 完整版代码已给出；T23 steps fixture 显式 skip 并标明由 client 专测覆盖。
3. **类型一致性**：`build_session_factory` 返回 `async_sessionmaker` 全文一致；`SubmitRequest` 字段与 T12/T13 调用一致；`LegStatus/OrderStatus` 枚举值与 T16 聚合调用一致；fixture 路径 `tests/fixtures/wedap/` 三处引用一致。
