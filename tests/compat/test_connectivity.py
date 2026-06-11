"""wedap dev 环境连通性验证（compat 套件入口）。

直连 wedap dev 环境，验证 HTTP 可达性。
无 GW_WEDAP_BASE_URL 环境变量时自动 skip（保证 CI 不误触发）。

运行方式：
    GW_WEDAP_BASE_URL=http://wedap-dev.internal:8021 pytest tests/compat -m compat --no-cov
"""

import os

import httpx
import pytest

_WEDAP_BASE_URL = os.environ.get("GW_WEDAP_BASE_URL", "")
_WEDAP_TIMEOUT = float(os.environ.get("GW_WEDAP_TIMEOUT", "10.0"))

pytestmark = pytest.mark.compat


@pytest.mark.compat
async def test_wedap_health_reachable() -> None:
    """验证 wedap dev 端点 HTTP 可达（GET /healthz 或任意路径返回非 5xx）。

    无 GW_WEDAP_BASE_URL 时 skip。
    """
    if not _WEDAP_BASE_URL:
        pytest.skip("GW_WEDAP_BASE_URL 未设置，跳过 wedap 连通性测试")

    async with httpx.AsyncClient(timeout=_WEDAP_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{_WEDAP_BASE_URL.rstrip('/')}/healthz",
                headers={"X-Caller-Service": "compat-test"},
            )
            # 任意非 5xx 视为 wedap 可达（4xx = 认证问题，但服务本身在线）
            assert resp.status_code < 500, f"wedap dev 返回 {resp.status_code}，服务可能不健康"
        except httpx.ConnectError as exc:
            pytest.fail(f"无法连接 wedap dev ({_WEDAP_BASE_URL}): {exc}")
        except httpx.TimeoutException as exc:
            pytest.fail(f"连接 wedap dev 超时 ({_WEDAP_TIMEOUT}s): {exc}")
