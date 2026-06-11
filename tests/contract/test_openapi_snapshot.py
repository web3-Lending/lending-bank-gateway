"""北向契约漂移门禁：OpenAPI 快照比对测试。

改动任何对外 API（路由、字段、schema）必须连带更新 contracts/openapi.json，
确保变更在评审时可见。
"""

import json
import pathlib

from fastapi.testclient import TestClient

from app.main import create_app

SNAPSHOT = pathlib.Path(__file__).parent.parent.parent / "contracts" / "openapi.json"

# /openapi.json 受 S2SMiddleware 保护，需提供最小有效头
_S2S_HEADERS = {"X-Caller-Service": "test-runner"}


def test_openapi_matches_snapshot() -> None:
    """北向契约漂移门禁：改契约必须连带更新 checked-in 快照（评审可见）。"""
    spec = TestClient(create_app()).get("/openapi.json", headers=_S2S_HEADERS).json()
    assert SNAPSHOT.exists(), "缺 contracts/openapi.json 基准——运行 scripts 生成并评审入库"
    assert spec == json.loads(SNAPSHOT.read_text()), (
        "北向契约漂移：先评审变更，再更新 contracts/openapi.json"
    )
