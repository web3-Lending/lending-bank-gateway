"""admin_ops 端点测试：outbox 重放（成功 / 不存在或非 DEAD）。"""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.base import Base
from app.models.callback import CallbackOutbox

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "admin-req-001",
}


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    return TestClient(app)


async def _create_tables(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _insert_dead_outbox(engine: Any) -> int:
    """插入一条 DEAD 状态的 outbox 行，返回 id。"""

    async with engine.connect() as conn:
        from sqlalchemy import insert

        result = await conn.execute(
            insert(CallbackOutbox).values(
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B9"},
                status="DEAD",
                attempts=3,
            )
        )
        await conn.commit()
        return result.inserted_primary_key[0]


def test_replay_dead_outbox_success(client: TestClient) -> None:
    """DEAD 行 replay → 200 ok，返回 replayed=outbox_id。"""
    oid = asyncio.run(_insert_dead_outbox(client.app.state.engine))  # type: ignore[union-attr]
    r = client.post(f"/api/v1/admin/outbox/{oid}/replay", headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["data"]["replayed"] == oid


def test_replay_nonexistent_outbox_404(client: TestClient) -> None:
    """不存在的 outbox_id → 404 GW_404_OUTBOX。"""
    r = client.post("/api/v1/admin/outbox/99999/replay", headers=HEADERS)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "GW_404_OUTBOX"


def test_replay_pending_outbox_404(client: TestClient) -> None:
    """PENDING（非 DEAD）行 replay → 404 GW_404_OUTBOX（replay_dead 返回 False）。"""

    # 插入一条 PENDING 行
    async def _insert_pending(engine: Any) -> int:
        from sqlalchemy import insert

        async with engine.connect() as conn:
            result = await conn.execute(
                insert(CallbackOutbox).values(
                    tenant_id="OCBC",
                    target="lifecycle",
                    payload={"bizSeqNo": "B8"},
                    status="PENDING",
                    attempts=0,
                )
            )
            await conn.commit()
            return result.inserted_primary_key[0]

    oid = asyncio.run(_insert_pending(client.app.state.engine))  # type: ignore[union-attr]
    r = client.post(f"/api/v1/admin/outbox/{oid}/replay", headers=HEADERS)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "GW_404_OUTBOX"
