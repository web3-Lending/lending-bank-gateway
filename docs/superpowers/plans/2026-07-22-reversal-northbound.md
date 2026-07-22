# 通用冲正北向端点（reversal）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 lending-bank-gateway 补一个北向"通用冲正"端点 `POST /api/v1/bank-funds/reversals`，对接 wedap 已实现的 `POST /api/v1/transactions/reversal`（Public.md §4.4.2），并启用全额退款护栏。

**Architecture:** 端点落一张 RVSL 台账单（复用幂等/账户守门/审计的既有原语）→ 同步调 wedap 通用冲正 → HTTP 200 即认冲正指令成功（RVSL 单 SUCCEEDED），在同一事务内把本地原单 SUCCEEDED→REVERSED（复用 `finalize_terminal_in_session` 升级路径，幂等）。不同于既有 callback 摄取链——通用冲正是同步权威返回、无回调。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy async、pytest、httpx。

## Global Constraints

- wedap 通用冲正契约（Public.md §4.4.2，逐字）：路径 `POST /api/v1/transactions/reversal`；**同步**返回 `txnStatus=REVERSED`，无回调；`transType`=**原交易类型**（一期仅 `BANK_FUND_COLLECT_LOAN`/`BANK_FUND_COLLECT_CLEARING`）；仅全额；防冲错 `oriTxnAmount`+`currencyCode` 三方一致，否则 422；幂等 X-Request-Id，已 REVERSED 重复冲正返回 REVERSED 不报错。
- 后端覆盖率门禁：行+分支达 gateway 质量门（`08-quality-gate.md`），新增代码全覆盖。
- lint/type：`ruff` + `mypy` 提交前跑绿（PostToolUse 会自动 ruff --fix，注意先加用法再加 import）。
- 金额一律 `str` 透传 + `parse_amount` 解析为 `Decimal`，禁 float。
- 不在主仓 main 写代码：本计划在 worktree `.wt-gateway/reversal-northbound`（分支 `feat/reversal-northbound`）执行。
- 提交遵循 conventional commits 中文 + `Co-Authored-By: Claude <noreply@anthropic.com>` trailer。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `app/clients/wedap.py` | Modify | 加 `reverse()` 方法，POST wedap 通用冲正 |
| `app/services/reversal.py` | Create | `submit_reversal()` 编排 + `_reverse_original()` 原单翻转 |
| `app/api/v1/bank_funds.py` | Modify | 加 `ReversalRequest` schema + `reverse_transaction` 端点 |
| `app/core/config.py` | Modify | `refund_full_amount_guard` 默认 `False`→`True` |
| `tests/api/test_reversal.py` | Create | 端点行为全覆盖 |
| `tests/services/test_reversal_service.py` | Create | `submit_reversal` 单元测试（原单翻转/幂等/失败分支）|
| `tests/api/test_refund.py` | Modify | 修 `guard_off_by_default` 测试（默认已翻 True）|
| `contracts/openapi.json` | Modify | 契约快照再生成（漂移门禁）|

---

## Task 1: wedap `reverse()` client 方法

**Files:**
- Modify: `app/clients/wedap.py`（在 `refund()` 方法之后，约 266 行）
- Test: `tests/clients/test_wedap_reverse.py`（Create）

**Interfaces:**
- Produces: `WedapClient.reverse(*, tenant_id: str, request_id: str, payload: dict[str, Any]) -> dict[str, Any]`，与 `refund()` 同签名。

- [ ] **Step 1: 写失败测试**

`tests/clients/test_wedap_reverse.py`：

```python
"""wedap 通用冲正 client 方法：reverse() → POST /api/v1/transactions/reversal。"""

import pytest
from unittest.mock import AsyncMock

from app.clients.wedap import WedapClient


@pytest.mark.asyncio
async def test_reverse_posts_to_general_reversal_path() -> None:
    client = WedapClient(base_url="http://wedap.test")
    client._post = AsyncMock(return_value={"txnStatus": "REVERSED", "bizSeqNo": "RVSL-1"})  # type: ignore[method-assign]
    payload = {
        "bizSeqNo": "RVSL-1",
        "transType": "BANK_FUND_COLLECT_LOAN",
        "oriBizSeqNo": "CLT-1",
        "oriReqDate": "20260722",
        "oriTxnAmount": "5000.0000",
        "currencyCode": "USD",
    }
    data = await client.reverse(tenant_id="WBTHK01", request_id="req-1", payload=payload)
    assert data["txnStatus"] == "REVERSED"
    client._post.assert_awaited_once()
    args, kwargs = client._post.call_args
    assert args[0] == "/api/v1/transactions/reversal"
    assert kwargs["tenant_id"] == "WBTHK01"
    assert kwargs["payload"] == payload
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/clients/test_wedap_reverse.py -v`
Expected: FAIL — `AttributeError: 'WedapClient' object has no attribute 'reverse'`

- [ ] **Step 3: 写实现**

在 `app/clients/wedap.py` 的 `refund()` 方法之后加：

```python
    async def reverse(
        self,
        *,
        tenant_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """通用冲正（对接文档 Public.md §4.4.2，一期已实现）：全额冲正原归集单。

        同步接口：成功同步返回 txnStatus=REVERSED（当日 DCN / 跨日 BANK-104），无回调。
        transType=原交易类型，一期仅归集类 BANK_FUND_COLLECT_LOAN/BANK_FUND_COLLECT_CLEARING；
        资金到客户账不支持冲正。防冲错：wedap 三方校验 oriTxnAmount/currencyCode，不一致返 422。
        幂等 X-Request-Id：已 REVERSED 重复冲正返回 REVERSED 不报错。gateway 薄透传不复刻业务校验。
        """
        return await self._post(
            "/api/v1/transactions/reversal",
            tenant_id=tenant_id,
            request_id=request_id,
            payload=payload,
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/clients/test_wedap_reverse.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/clients/wedap.py tests/clients/test_wedap_reverse.py
git commit -m "feat(gateway): wedap reverse() 通用冲正 client 方法

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: `submit_reversal` 编排服务

**Files:**
- Create: `app/services/reversal.py`
- Test: `tests/services/test_reversal_service.py`（Create）

**Interfaces:**
- Consumes: `SubmitRequest`（`app/services/submit.py`）；`finalize_terminal_in_session`、`is_terminal`（`app/services/order_finalize.py`）；`check_or_register`、`record_response`、`IdempotencyConflict`、`IdempotencyInFlight`（`app/services/idempotency.py`）；`OrderStatus`、`assert_transition`、`IllegalTransition`（`app/domain/states.py`）；`BankTxnOrder`（`app/models/txn.py`）；`validate_biz_seq_no`（`app/domain/biz_seq.py`）；`write_audit`（`app/services/audit.py`）；`WedapError`（`app/clients/wedap.py`）。
- Produces: `async def submit_reversal(factory, *, wedap_reverse: Callable[..., Awaitable[dict[str, Any]]], req: SubmitRequest, ori_biz_seq_no: str) -> dict[str, Any]`。

> **设计说明（对齐 spec §4.2）**：不复用 `submit_order`——`submit_order` 把 wedap 响应 `txnStatus` 映射到提交单，而通用冲正返回的 `REVERSED` 描述的是**原单**状态、非冲正指令成败。故独立编排：HTTP 200 → RVSL 单 SUCCEEDED（指令受理成功），原单在同一事务翻 REVERSED。幂等/账户守门等硬核逻辑仍复用共享原语（`check_or_register`/`record_response`/`finalize_terminal_in_session`），只有编排骨架与 `submit_order` 平行（两者若改幂等语义需同步）。

- [ ] **Step 1: 写失败测试（原单翻转 + RVSL 落 SUCCEEDED）**

`tests/services/test_reversal_service.py`：

```python
"""submit_reversal 单元测试：RVSL 单 SUCCEEDED + 原单同步翻 REVERSED。"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.states import OrderStatus
from app.models.base import Base
from app.models.txn import BankTxnOrder
from app.services.reversal import submit_reversal
from app.services.submit import SubmitRequest


@pytest.fixture()
def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init() -> async_sessionmaker:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return async_sessionmaker(engine, expire_on_commit=False)

    return asyncio.run(_init())


async def _seed_succeeded_collect(factory, biz: str) -> None:
    async with factory() as s:
        async with s.begin():
            s.add(
                BankTxnOrder(
                    tenant_id="WBTHK01",
                    biz_seq_no=biz,
                    business_action="COLLECT",
                    biz_type="COLL",
                    amount=Decimal("5000.0000"),
                    currency="USD",
                    caller_service="liquidation",
                    status=OrderStatus.SUCCEEDED,
                    request_id="req-collect",
                    trans_type="BANK_FUND_COLLECT_LOAN",
                )
            )


def _req(rvsl: str) -> SubmitRequest:
    return SubmitRequest(
        tenant_id="WBTHK01",
        biz_seq_no=rvsl,
        business_action="REVERSE",
        biz_type="RVSL",
        amount=Decimal("5000.0000"),
        currency="USD",
        caller_service="liquidation",
        request_id="req-rvsl",
        business_scope="bank_reversal",
        wedap_payload={"transType": "BANK_FUND_COLLECT_LOAN", "oriBizSeqNo": "CLT-1"},
        ori_req_date="20260722",
    )


def test_submit_reversal_flips_original_and_lands_rvsl_succeeded(factory) -> None:  # type: ignore[no-untyped-def]
    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-1")
        wedap_reverse = AsyncMock(return_value={"txnStatus": "REVERSED", "bizSeqNo": "RVSL-1"})
        resp = await submit_reversal(
            factory, wedap_reverse=wedap_reverse, req=_req("RVSL-1"), ori_biz_seq_no="CLT-1"
        )
        assert resp["txnStatus"] == "REVERSED"
        async with factory() as s:
            rvsl = (await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-1"))).scalar_one()
            ori = (await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-1"))).scalar_one()
        assert rvsl.status == OrderStatus.SUCCEEDED  # 冲正指令受理成功
        assert ori.status == OrderStatus.REVERSED    # 原单被翻转

    asyncio.run(_run())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/services/test_reversal_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.reversal'`

- [ ] **Step 3: 写实现**

`app/services/reversal.py`：

```python
"""通用冲正编排：RVSL 台账单 SUCCEEDED + 原单同步翻 REVERSED（Public.md §4.4.2 同步无回调）。

与 submit_order 平行但语义不同：wedap 通用冲正 HTTP 200 即冲正指令受理成功（RVSL 单
SUCCEEDED），响应 txnStatus=REVERSED 描述的是**原单**新态——故在同一事务内把本地原单
SUCCEEDED→REVERSED（复用 finalize_terminal_in_session 升级路径，幂等）。幂等/记账等硬核
逻辑复用共享原语；两者若改幂等语义需同步维护。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.clients.wedap import WedapError
from app.domain.biz_seq import validate_biz_seq_no
from app.domain.states import IllegalTransition, OrderStatus, assert_transition
from app.models.txn import BankTxnOrder
from app.services.audit import write_audit
from app.services.idempotency import (
    IdempotencyConflict,
    IdempotencyInFlight,
    check_or_register,
    record_response,
)
from app.services.order_finalize import finalize_terminal_in_session, is_terminal
from app.services.submit import SubmitRequest

logger = logging.getLogger(__name__)


async def _reverse_original(
    session: Any,
    *,
    tenant_id: str,
    ori_biz_seq_no: str,
    trace_id: str,
    caller_service: str,
) -> None:
    """本地原单同步翻 REVERSED（复用升级 helper，幂等）。查不到不拦；已 REVERSED 跳过；
    非法转移（原单 FAILED/CANCELLED/EXPIRED，与 wedap 权威 REVERSED 分歧）记警告不翻、不炸请求。"""
    ori = (
        await session.execute(
            select(BankTxnOrder)
            .where(BankTxnOrder.tenant_id == tenant_id, BankTxnOrder.biz_seq_no == ori_biz_seq_no)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if ori is None:
        return  # 查不到不拦（原单可能非本 gateway 出，同 refund 口径）
    if OrderStatus(ori.status) == OrderStatus.REVERSED:
        return  # 幂等：已冲正
    try:
        assert_transition(OrderStatus(ori.status), OrderStatus.REVERSED)
    except IllegalTransition:
        logger.warning(
            "original %s/%s in %s cannot flip to REVERSED (wedap authoritative, skip local flip)",
            tenant_id, ori_biz_seq_no, ori.status,
        )
        return
    upgrade_from = str(ori.status)
    ori.status = OrderStatus.REVERSED
    await finalize_terminal_in_session(
        session,
        order=ori,
        source="SYNC",
        trace_id=trace_id,
        caller_service=caller_service,
        upgrade_from=upgrade_from,
    )


async def submit_reversal(
    factory: Any,
    *,
    wedap_reverse: Callable[..., Awaitable[dict[str, Any]]],
    req: SubmitRequest,
    ori_biz_seq_no: str,
) -> dict[str, Any]:
    """受理：事务1 幂等+RVSL(ACCEPTED) 落库（禁外呼）→ wedap 冲正外呼 → 事务2 RVSL 推进 + 原单翻转。"""
    validate_biz_seq_no(req.biz_seq_no)

    # 事务1：check_or_register + RVSL(ACCEPTED) 落库
    try:
        async with factory() as session:
            try:
                async with session.begin():
                    hit = await check_or_register(
                        session,
                        tenant_id=req.tenant_id,
                        business_scope=req.business_scope,
                        idempotency_key=req.biz_seq_no,
                        method="POST",
                        path=req.business_scope,
                        payload=req.wedap_payload,
                    )
                    if hit is not None:
                        return hit
                    session.add(
                        BankTxnOrder(
                            tenant_id=req.tenant_id,
                            biz_seq_no=req.biz_seq_no,
                            business_action=req.business_action,
                            biz_type=req.biz_type,
                            amount=req.amount,
                            currency=req.currency,
                            caller_service=req.caller_service,
                            status=OrderStatus.ACCEPTED,
                            request_id=req.request_id,
                            trans_type=(str(req.wedap_payload.get("transType") or "") or None),
                            ori_req_date=req.ori_req_date,
                        )
                    )
            except IntegrityError:
                logger.error(
                    "reversal order exists without idempotency record: %s/%s",
                    req.tenant_id, req.biz_seq_no,
                )
                raise IdempotencyConflict(req.biz_seq_no) from None
    except IdempotencyInFlight:
        return {"txnStatus": "PROCESSING", "bizSeqNo": req.biz_seq_no, "inFlight": True}

    # 外呼：HTTP 200 → 冲正指令受理成功（RVSL SUCCEEDED）；超时/5xx→RESULT_UNKNOWN；4xx/WedapError→FAILED
    try:
        data = await wedap_reverse(
            tenant_id=req.tenant_id, request_id=req.request_id, payload=req.wedap_payload
        )
        new_status = OrderStatus.SUCCEEDED
        response: dict[str, Any] = {
            "txnStatus": data.get("txnStatus", "REVERSED"),
            "bizSeqNo": req.biz_seq_no,
        }
    except (httpx.TimeoutException, httpx.TransportError):
        new_status = OrderStatus.RESULT_UNKNOWN
        response = {"txnStatus": "RESULT_UNKNOWN", "bizSeqNo": req.biz_seq_no}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code >= 500:
            new_status = OrderStatus.RESULT_UNKNOWN
            response = {"txnStatus": "RESULT_UNKNOWN", "bizSeqNo": req.biz_seq_no}
        else:
            new_status = OrderStatus.FAILED
            response = {
                "txnStatus": "FAILED",
                "bizSeqNo": req.biz_seq_no,
                "errorCode": f"HTTP_{exc.response.status_code}",
            }
    except WedapError as exc:
        new_status = OrderStatus.FAILED
        response = {
            "txnStatus": "FAILED",
            "bizSeqNo": req.biz_seq_no,
            "errorCode": exc.code,
            "errorMsg": exc.msg[:200],
        }

    # 事务2：CAS 推进 RVSL + 原单翻转 + record_response
    assert_transition(OrderStatus.ACCEPTED, new_status)
    now = dt.datetime.now(dt.UTC)
    async with factory() as session:
        async with session.begin():
            rvsl = (
                await session.execute(
                    select(BankTxnOrder)
                    .where(
                        BankTxnOrder.tenant_id == req.tenant_id,
                        BankTxnOrder.biz_seq_no == req.biz_seq_no,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if rvsl.status == OrderStatus.ACCEPTED:
                rvsl.status = new_status
                rvsl.submitted_at = now
                if is_terminal(new_status):
                    await finalize_terminal_in_session(
                        session,
                        order=rvsl,
                        source="SYNC",
                        trace_id=req.request_id,
                        caller_service=req.caller_service,
                    )
                else:
                    await write_audit(
                        session,
                        tenant_id=req.tenant_id,
                        actor=f"svc:{req.caller_service}",
                        action=f"ORDER_{new_status}",
                        entity=f"bank_txn_order:{req.biz_seq_no}",
                        payload={"business_action": req.business_action, "amount": str(req.amount)},
                    )
                # 原单翻转仅在冲正受理成功时执行
                if new_status == OrderStatus.SUCCEEDED:
                    await _reverse_original(
                        session,
                        tenant_id=req.tenant_id,
                        ori_biz_seq_no=ori_biz_seq_no,
                        trace_id=req.request_id,
                        caller_service=req.caller_service,
                    )
            await record_response(
                session,
                tenant_id=req.tenant_id,
                business_scope=req.business_scope,
                idempotency_key=req.biz_seq_no,
                response=response,
                final_effect_id=f"order:{req.biz_seq_no}",
            )
    return response
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/services/test_reversal_service.py -v`
Expected: PASS

- [ ] **Step 5: 加补充分支测试（查不到不拦 / wedap 失败不翻原单 / 幂等重放）**

追加到 `tests/services/test_reversal_service.py`：

```python
def test_submit_reversal_no_local_original_does_not_raise(factory) -> None:  # type: ignore[no-untyped-def]
    async def _run() -> None:
        wedap_reverse = AsyncMock(return_value={"txnStatus": "REVERSED"})
        resp = await submit_reversal(
            factory, wedap_reverse=wedap_reverse, req=_req("RVSL-NOORI"), ori_biz_seq_no="CLT-ABSENT"
        )
        assert resp["txnStatus"] == "REVERSED"
        async with factory() as s:
            rvsl = (await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-NOORI"))).scalar_one()
        assert rvsl.status == OrderStatus.SUCCEEDED

    asyncio.run(_run())


def test_submit_reversal_wedap_error_fails_and_keeps_original(factory) -> None:  # type: ignore[no-untyped-def]
    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-KEEP")
        wedap_reverse = AsyncMock(side_effect=WedapError("BANK_08", "交易不存在"))
        resp = await submit_reversal(
            factory, wedap_reverse=wedap_reverse, req=_req("RVSL-ERR"), ori_biz_seq_no="CLT-KEEP"
        )
        assert resp["txnStatus"] == "FAILED"
        assert resp["errorCode"] == "BANK_08"
        async with factory() as s:
            ori = (await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-KEEP"))).scalar_one()
        assert ori.status == OrderStatus.SUCCEEDED  # 冲正失败，原单不翻

    asyncio.run(_run())


def test_submit_reversal_idempotent_replay(factory) -> None:  # type: ignore[no-untyped-def]
    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-IDEM")
        wedap_reverse = AsyncMock(return_value={"txnStatus": "REVERSED"})
        r1 = await submit_reversal(factory, wedap_reverse=wedap_reverse, req=_req("RVSL-IDEM"), ori_biz_seq_no="CLT-IDEM")
        r2 = await submit_reversal(factory, wedap_reverse=wedap_reverse, req=_req("RVSL-IDEM"), ori_biz_seq_no="CLT-IDEM")
        assert r1["txnStatus"] == "REVERSED"
        assert r2 == r1  # 重放返回首次 response
        assert wedap_reverse.await_count == 1  # 零重复外呼

    asyncio.run(_run())
```

- [ ] **Step 6: 跑全部 service 测试确认通过**

Run: `pytest tests/services/test_reversal_service.py -v`
Expected: PASS（4 用例全绿）

- [ ] **Step 7: 提交**

```bash
git add app/services/reversal.py tests/services/test_reversal_service.py
git commit -m "feat(gateway): submit_reversal 编排——RVSL 单 + 原单同步翻 REVERSED

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: `ReversalRequest` schema + `reverse_transaction` 端点

**Files:**
- Modify: `app/api/v1/bank_funds.py`（`RefundRequest` 后加 schema；`refund_to_user` 后加端点）
- Test: `tests/api/test_reversal.py`（Create）

**Interfaces:**
- Consumes: `submit_reversal`（Task 2）；`WedapClient.reverse`（Task 1，经 `request.app.state.wedap`）；既有 `require_headers`、`parse_amount`、`assert_idempotency_key_matches`、`bank_req_date`（`app/api/deps.py`）；`ok`（`app/core/envelope.py`）。
- Produces: 路由 `POST /api/v1/bank-funds/reversals`。

- [ ] **Step 1: 写失败测试**

`tests/api/test_reversal.py`：

```python
"""北向通用冲正 API 测试：POST /api/v1/bank-funds/reversals（对接 wedap Public.md §4.4.2）。"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.domain.states import OrderStatus
from app.main import create_app
from app.models.base import Base
from app.models.txn import BankTxnOrder

RVSL = "RVSL-20260722-0001234567890"
COLL = "CLT-20260722-0001234567890"
HEADERS = {
    "X-Caller-Service": "liquidation",
    "X-Tenant-Id": "WBTHK01",
    "X-Request-Id": "req-rvsl-1",
    "Idempotency-Key": RVSL,
}
REVERSAL_BODY = {
    "bizSeqNo": RVSL,
    "channelId": "W3C",
    "transType": "BANK_FUND_COLLECT_LOAN",
    "oriBizSeqNo": COLL,
    "oriReqDate": "20260722",
    "oriTxnAmount": "5000.0000",
    "currencyCode": "USD",
    "reason": "abort 全额冲正",
}


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_collect(factory) -> None:  # type: ignore[no-untyped-def]
    async with factory() as s:
        async with s.begin():
            s.add(
                BankTxnOrder(
                    tenant_id="WBTHK01", biz_seq_no=COLL, business_action="COLLECT",
                    biz_type="COLL", amount=Decimal("5000.0000"), currency="USD",
                    caller_service="liquidation", status=OrderStatus.SUCCEEDED,
                    request_id="req-coll", trans_type="BANK_FUND_COLLECT_LOAN",
                )
            )


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    asyncio.run(_seed_collect(app.state.session_factory))
    wedap = AsyncMock()
    wedap.reverse.return_value = {"txnStatus": "REVERSED", "bizSeqNo": RVSL, "reversalBizSeqNo": "R-1"}
    app.state.wedap = wedap
    return TestClient(app)


def test_reversal_succeeds_and_flips_original(client: TestClient) -> None:
    r = client.post("/api/v1/bank-funds/reversals", json=REVERSAL_BODY, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["data"]["txnStatus"] == "REVERSED"

    async def _check() -> tuple[str, str]:
        async with client.app.state.session_factory() as s:  # type: ignore[union-attr]
            rvsl = (await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == RVSL))).scalar_one()
            ori = (await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == COLL))).scalar_one()
            return rvsl.status, ori.status

    rvsl_status, ori_status = asyncio.run(_check())
    assert rvsl_status == OrderStatus.SUCCEEDED
    assert ori_status == OrderStatus.REVERSED


def test_reversal_passes_original_transtype_to_wedap(client: TestClient) -> None:
    client.post("/api/v1/bank-funds/reversals", json=REVERSAL_BODY, headers=HEADERS)
    _, kwargs = client.app.state.wedap.reverse.call_args  # type: ignore[union-attr]
    assert kwargs["payload"]["transType"] == "BANK_FUND_COLLECT_LOAN"
    assert kwargs["payload"]["oriBizSeqNo"] == COLL


def test_reversal_missing_ori_biz_seq_no_422(client: TestClient) -> None:
    body = {k: v for k, v in REVERSAL_BODY.items() if k != "oriBizSeqNo"}
    r = client.post("/api/v1/bank-funds/reversals", json=body, headers=HEADERS)
    assert r.status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/api/test_reversal.py -v`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 写 schema**

在 `app/api/v1/bank_funds.py` 的 `RefundRequest` 之后加：

```python
class ReversalRequest(BaseModel):
    # 通用冲正薄透传（对接文档 Public.md §4.4.2）：全额冲正原归集单，gateway 只取记账/幂等
    # /原单翻转所需最少键；channelId/oriRequestId/bankAccountNo 等经 extra=allow 原样透传。
    # 三方防冲错校验（oriTxnAmount/currencyCode == 本地原单 == BANK-313）在 wedap 侧，gateway 不复刻。
    model_config = ConfigDict(extra="allow")
    bizSeqNo: str
    currencyCode: str
    # transType=原交易类型（一期归集类），wedap 按 (oriBizSeqNo, transType) 消歧；≤20 落库供回查。
    transType: str = Field(min_length=1, max_length=20)
    oriBizSeqNo: str
    """被冲正的原交易流水号；本地查得到则同步翻 REVERSED，查不到不拦（同 refund）。"""
    oriTxnAmount: str
    """原交易金额（wedap 防冲错校验用，非可调冲正金额）。"""
```

- [ ] **Step 4: 写端点**

在 `app/api/v1/bank_funds.py` 的 `refund_to_user` 之后加（顶部 import 加 `from app.services.reversal import submit_reversal`）：

```python
@router.post("/reversals")
async def reverse_transaction(
    body: ReversalRequest,
    request: Request,
    ids: dict[str, str] = Depends(require_headers),
) -> dict[str, Any]:
    """通用冲正北向端点（对接 wedap Public.md §4.4.2，全额冲正原归集单）。

    落 RVSL 单（biz_type=RVSL）→ 同步调 wedap 通用冲正 → HTTP 200 即指令受理成功
    （RVSL 单 SUCCEEDED），同一事务把本地原单 SUCCEEDED→REVERSED（查不到不拦）。
    通用冲正同步无回调，不走既有 callback 摄取链。仅全额冲正，部分退款走 /refunds。
    """
    assert_idempotency_key_matches(request, body.bizSeqNo)
    amount = parse_amount(body.oriTxnAmount, body.currencyCode)
    # 账户守门：冲正把资金退回原付款方，与 refund 同向，须过 platform_account 守门。
    await assert_platform_account_allowed(
        request.app.state.session_factory,
        body.model_dump(mode="json", exclude_none=True).get("bankAccountNo"),
        tenant_id=ids["tenant_id"],
        business_scope="bank_reversal",
        currency=body.currencyCode,
        caller=ids["caller_service"],
        trace_id=ids["trace_id"],
        mode=request.app.state.settings.account_guard_mode,
    )
    try:
        result = await submit_reversal(
            request.app.state.session_factory,
            wedap_reverse=request.app.state.wedap.reverse,
            req=SubmitRequest(
                tenant_id=ids["tenant_id"],
                biz_seq_no=body.bizSeqNo,
                business_action="REVERSE",
                biz_type="RVSL",
                amount=amount,
                currency=body.currencyCode,
                caller_service=ids["caller_service"],
                request_id=ids["request_id"],
                business_scope="bank_reversal",
                wedap_payload=body.model_dump(mode="json", exclude_none=True),
                ori_req_date=bank_req_date(request),
            ),
            ori_biz_seq_no=body.oriBizSeqNo,
        )
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "GW_400_VALIDATION", "message": str(exc)}) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(
            409, detail={"code": "GW_409_IDEMPOTENCY", "message": f"idempotency conflict: {exc}"}
        ) from exc
    return ok(result, trace_id=ids["trace_id"])
```

> 说明：`SubmitRequest` 已在文件顶部 `from app.services.submit import SubmitRequest, submit_order` 导入，只需新增 `submit_reversal` import。账户守门在端点内先跑（与 `_submit` 一致），故 `submit_reversal` 不重复守门。

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/api/test_reversal.py -v`
Expected: PASS（3 用例全绿）

- [ ] **Step 6: 跑 ruff + mypy**

Run: `ruff check app/api/v1/bank_funds.py app/services/reversal.py && mypy app/services/reversal.py app/api/v1/bank_funds.py`
Expected: 无错误

- [ ] **Step 7: 提交**

```bash
git add app/api/v1/bank_funds.py tests/api/test_reversal.py
git commit -m "feat(gateway): POST /bank-funds/reversals 通用冲正北向端点

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: 护栏默认翻 True + 修 refund 测试

**Files:**
- Modify: `app/core/config.py:57`
- Modify: `tests/api/test_refund.py`（`test_refund_full_amount_guard_off_by_default`，约 137 行）

**Interfaces:**
- Consumes: 无新接口。改 `Settings.refund_full_amount_guard` 默认值。

> **行为变更告警**：默认翻 True 后，凡本地能查到原单、且 refund 金额==原单金额的**全额退款一律 422**。调用方（liquidation）须同步把全额路由改到 reversal，否则联调红（spec §4.4/§8）。

- [ ] **Step 1: 改默认值 + 更新注释**

`app/core/config.py:57`，把：

```python
    refund_full_amount_guard: bool = False
```

改为：

```python
    # 退款分流护栏（FU-GW-REVERSAL-INGESTION · 用户拍板 2026-07-15：全额退款走冲正、
    # refund 仅部分退款）。默认开（2026-07-22 通用冲正端点落地后翻 True）：refundAmount ==
    # 原归集单金额的全额退款被 422 导流 /bank-funds/reversals；部分退款不受影响。
    refund_full_amount_guard: bool = True
```

（同时删除原第 54-56 行"默认关：wedap 冲正 4.8 未落地前…"三行旧注释，避免与新默认矛盾。）

- [ ] **Step 2: 跑既有 refund 测试确认 off_by_default 断裂**

Run: `pytest tests/api/test_refund.py::test_refund_full_amount_guard_off_by_default -v`
Expected: FAIL —— 默认已 True，全额 refund 现返 422 而非 200（该测试断言 200）

- [ ] **Step 3: 改测试为"显式关闭才放行"**

把 `tests/api/test_refund.py` 的 `test_refund_full_amount_guard_off_by_default`（约 137 行）整体替换为：

```python
def test_refund_full_amount_guard_off_when_explicitly_disabled(client: TestClient) -> None:
    """flag 显式关：全额退款（== 原单金额）不被 gateway 拦，照常提交 wedap（过渡口径）。"""
    client.app.state.settings.refund_full_amount_guard = False
    try:
        coll = {
            "bizSeqNo": "CLT-20260715-GUARD-OFF-01",
            "transType": "LOAN_COLLECT",
            "txnAmount": "1.00",
            "currencyCode": "USD",
        }
        client.app.state.wedap.collect_from_users = AsyncMock(  # type: ignore[union-attr]
            return_value={"txnStatus": "SUCCESS"}
        )
        h = {**HEADERS, "Idempotency-Key": coll["bizSeqNo"], "X-Request-Id": "req-goff-c"}
        client.post("/api/v1/bank-funds/collect-from-users", json=coll, headers=h)
        body = {
            **REFUND_BODY,
            "bizSeqNo": "RFD-20260715-GUARD-OFF-01",
            "oriBizSeqNo": coll["bizSeqNo"],
            "refundAmount": "1.00",  # == 原单金额（全额）
        }
        h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-goff-r"}
        r = client.post("/api/v1/bank-funds/refunds", json=body, headers=h)
        assert r.status_code == 200
        assert r.json()["data"]["txnStatus"] == "SUCCESS"
    finally:
        client.app.state.settings.refund_full_amount_guard = True
```

- [ ] **Step 4: 加"默认即拦"覆盖**

在上面测试之后追加：

```python
def test_refund_full_amount_guard_on_by_default_rejects(client: TestClient) -> None:
    """默认（True）：能查到原单的全额退款被 422 导流冲正。"""
    coll = {
        "bizSeqNo": "CLT-20260722-GUARD-DEF-01",
        "transType": "LOAN_COLLECT",
        "txnAmount": "1.00",
        "currencyCode": "USD",
    }
    client.app.state.wedap.collect_from_users = AsyncMock(  # type: ignore[union-attr]
        return_value={"txnStatus": "SUCCESS"}
    )
    h = {**HEADERS, "Idempotency-Key": coll["bizSeqNo"], "X-Request-Id": "req-gdef-c"}
    client.post("/api/v1/bank-funds/collect-from-users", json=coll, headers=h)
    body = {
        **REFUND_BODY,
        "bizSeqNo": "RFD-20260722-GUARD-DEF-01",
        "oriBizSeqNo": coll["bizSeqNo"],
        "refundAmount": "1.00",
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-gdef-r"}
    r = client.post("/api/v1/bank-funds/refunds", json=body, headers=h)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "GW_422_FULL_REFUND_USE_REVERSAL"
```

- [ ] **Step 5: 跑全部 refund 测试确认通过**

Run: `pytest tests/api/test_refund.py -v`
Expected: PASS（含新增两用例）

- [ ] **Step 6: 提交**

```bash
git add app/core/config.py tests/api/test_refund.py
git commit -m "feat(gateway): refund_full_amount_guard 默认翻 True（全额退款导流冲正）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: 再生成 OpenAPI 契约快照

**Files:**
- Modify: `contracts/openapi.json`
- Test: `tests/contract/test_openapi_snapshot.py`（既有，不改）

- [ ] **Step 1: 跑快照测试确认漂移**

Run: `pytest tests/contract/test_openapi_snapshot.py -v`
Expected: FAIL —— 新增 `/api/v1/bank-funds/reversals` 未入快照

- [ ] **Step 2: 再生成快照**

Run:
```bash
python -c "
from fastapi.testclient import TestClient
from app.main import create_app
import json, pathlib
spec = TestClient(create_app()).get('/openapi.json', headers={'X-Caller-Service': 'test-runner'}).json()
pathlib.Path('contracts/openapi.json').write_text(json.dumps(spec, ensure_ascii=False, indent=2) + '\n')
print('regenerated')
"
```

> 注意：若既有快照缩进/尾换行与上式不同，先 `git diff contracts/openapi.json` 确认**只有 reversals 相关新增**、无既有端点意外 diff；如格式整体漂移，改用与既有快照一致的 dump 参数（对齐 `test_openapi_snapshot.py` 的 `json.dumps` 口径）重生成。

- [ ] **Step 3: 跑快照测试确认通过**

Run: `pytest tests/contract/test_openapi_snapshot.py -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add contracts/openapi.json
git commit -m "chore(gateway): openapi 快照纳入 /bank-funds/reversals

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: 全量门禁 + 本地冒烟

**Files:** 无新增；跑质量门与本地起服务验收。

- [ ] **Step 1: 全量单测 + 覆盖率**

Run: `pytest --cov=app --cov-report=term-missing`
Expected: PASS，新增 `app/services/reversal.py` + 端点/schema 行分支全覆盖，总覆盖率达门禁底线。

- [ ] **Step 2: lint + type 全量**

Run: `ruff check . && mypy app`
Expected: 无错误

- [ ] **Step 3: 本地起服务冒烟（deploy 前置门禁：local 先行）**

Run: `cd deploy && bash deploy.sh`（或本地 compose 起 8022），起后 `curl` 冒烟：
```bash
curl -s -X POST http://localhost:8022/api/v1/bank-funds/reversals \
  -H 'X-Caller-Service: liquidation' -H 'X-Tenant-Id: WBTHK01' \
  -H 'X-Request-Id: smoke-rvsl-1' -H 'Idempotency-Key: RVSL-SMOKE-1' \
  -H 'Content-Type: application/json' \
  -d '{"bizSeqNo":"RVSL-SMOKE-1","channelId":"W3C","transType":"BANK_FUND_COLLECT_LOAN","oriBizSeqNo":"CLT-SMOKE-1","oriReqDate":"20260722","oriTxnAmount":"1.00","currencyCode":"USD"}'
```
Expected: 端点可达、返回结构化 envelope（wedap 未接时降级/错误码正常，不 500 崩）。

- [ ] **Step 4: 提交（若冒烟发现修正）**

按需 commit；无修正则跳过。

---

## Task 7: dev-hw 部署 + wedap dev 联调验收（real-host-verify）

> 本任务是真机验证，不在 TDD 单测环内。走 `~/.claude/refs/deploy-verification.md` (a)/(b)/(c)，用 `cc-followup --category verification` 登记。

- [ ] **Step 1:** dev-hw 部署 gateway（`/dev-hw-ssh` + gateway buildops 发布链）。
- [ ] **Step 2:** wedap dev 真发一笔归集（collect）→ 成功 SUCCEEDED → 调 `/bank-funds/reversals` 全额冲正 → 同步返回 REVERSED → `GET /bank-funds/status?bizSeqNo=` 查原单为 REVERSED 闭环。
- [ ] **Step 3:** 验幂等：同 RVSL bizSeqNo 重放 → 幂等不重复冲正。
- [ ] **Step 4:** 验非归集类 transType → wedap 422 被 gateway 透传（不 500）。
- [ ] **Step 5:** 用 `cc-followup complete` 落 evidence（curl 输出 + status 查单 + 原单 REVERSED）。

---

## Self-Review

**Spec coverage（对 spec 各节）：**
- §2 wedap 契约 → Task 1（reverse client）+ Task 3（schema 字段 transType/oriTxnAmount/oriBizSeqNo）✅
- §3 数据流（RVSL 单 + 同步翻原单）→ Task 2 ✅
- §4.1 schema → Task 3 Step 3 ✅
- §4.2 端点 + 原单同步翻转 → Task 2（`_reverse_original`）+ Task 3 ✅
- §4.3 wedap.reverse() → Task 1 ✅
- §4.4 护栏默认 True → Task 4 ✅
- §5 错误/幂等（409/400/422/降级）→ Task 2 分支 + Task 3 端点异常映射 + Task 2 Step 5 幂等测试 ✅
- §6 测试矩阵 → Task 2/3/4 各用例（成功翻转/查不到不拦/幂等/账户守门/wedap失败/护栏开关）✅
  - 注：账户守门 fail-closed 用例——既有 `tests/api/test_account_guard.py` 覆盖 `_submit` 路径；reversal 端点走同一 `assert_platform_account_allowed`，Task 3 可补一条 enforce 拒绝用例（可选，守门逻辑已被守门测试覆盖）。
- §7 范围外 → 计划不含法币/划转/建户/费用冲正 ✅
- §8 依赖（调用方传 transType/oriTxnAmount、护栏行为变更）→ Task 3 schema 必填 + Task 4 告警 ✅
- §9 落地顺序 → Task 1-7 对应 ✅

**Placeholder scan：** 无 TBD/TODO；每个 code step 有完整代码。Task 5 Step 2 的"若格式漂移"是运行时校验指引，非占位。

**Type consistency：** `submit_reversal(factory, *, wedap_reverse, req: SubmitRequest, ori_biz_seq_no)` 在 Task 2 定义、Task 3 端点按此调用一致；`WedapClient.reverse(*, tenant_id, request_id, payload)` Task 1 定义、Task 3 经 `app.state.wedap.reverse` 传入一致；`biz_type="RVSL"`/`business_action="REVERSE"`/`business_scope="bank_reversal"` 全任务一致。
