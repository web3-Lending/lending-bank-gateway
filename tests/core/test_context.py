"""app/core/context.py：关联 id 校验/重签、全响应 no-store、request-target 8,192-byte 预算。

对应 v2.2：API-HTTP-013 + §7.4/§11.1（关联 id 不可信）、API-HTTP-015（缓存）、
API-HTTP-003/006 + §7.2.1 第 1 步（URI 预算 → 414）。
"""

import importlib
import logging
import pkgutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.context import (
    CORRELATION_ID_MAX_LEN,
    MAX_REQUEST_TARGET_BYTES,
    request_target_bytes,
    sanitize_correlation_id,
)

HOSTILE_ID = 'evil<script>"; DROP--'


# ── sanitize_correlation_id ───────────────────────────────────────────────────


def test_missing_header_stays_none() -> None:
    """header 没传 → None（保留「没传」与「传了畸形值」的区别，不掩盖既有 400）。"""
    assert sanitize_correlation_id(None, prefix="req") is None


def test_safe_value_is_reused_verbatim() -> None:
    """合规值原样复用——跨服务链路靠它对得上，不能无脑重签。"""
    assert sanitize_correlation_id("trc-a.b:c_ok-1", prefix="trc") == "trc-a.b:c_ok-1"


def test_max_length_value_accepted() -> None:
    """恰好 64 字符仍是合规值（边界不能收紧一格）。"""
    value = "a" * CORRELATION_ID_MAX_LEN
    assert sanitize_correlation_id(value, prefix="req") == value


def test_overlong_value_is_resigned() -> None:
    """65 字符 → 重签；否则会在最窄的 String(64) 列上截断甚至撞约束变 500。"""
    resigned = sanitize_correlation_id("a" * (CORRELATION_ID_MAX_LEN + 1), prefix="req")
    assert resigned is not None
    assert resigned.startswith("req-") and len(resigned) == 4 + 32


def test_hostile_charset_is_resigned() -> None:
    """含引号/尖括号/空格的注入串 → 重签，原值一个字符都不留。"""
    resigned = sanitize_correlation_id(HOSTILE_ID, prefix="trc")
    assert resigned is not None
    assert resigned.startswith("trc-")
    assert "script" not in resigned and '"' not in resigned


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_blank_value_is_treated_as_absent(raw: str) -> None:
    """空串/纯空白等同「没传」→ None，绝不能重签。

    HTTP 语义上「header 存在且为空」等于「未提供」（RFC 9110 §5.5 前后空白无意义）。
    若这里重签成受控值，`require_headers` 的 `if not ids.request_id` 就不再命中，
    `X-Request-Id: ` 会从既有 400 静默变 200——本波严禁改状态码取值。
    """
    assert sanitize_correlation_id(raw, prefix="req") is None


def test_max_len_matches_narrowest_persisted_column() -> None:
    """上限必须 <= 全部落库列宽的最小值，否则通过校验的值仍会被 DB 截断。

    机器核对真实 ORM 定义，防「以后有人把某张表的 request_id 收窄到 32 而这里没跟着改」。
    """
    import app.models
    from app.models.base import Base

    for module in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"app.models.{module.name}")
    widths = [
        column.type.length
        for table in Base.metadata.tables.values()
        for name in ("request_id", "trace_id")
        if (column := table.columns.get(name)) is not None and getattr(column.type, "length", None)
    ]
    assert widths, "未采集到任何 request_id/trace_id 列，采集逻辑失效"
    assert CORRELATION_ID_MAX_LEN <= min(widths)


# ── request_target_bytes ──────────────────────────────────────────────────────


def test_target_bytes_counts_path_only() -> None:
    assert request_target_bytes({"raw_path": b"/healthz", "query_string": b""}) == 8


def test_target_bytes_counts_question_mark_and_query() -> None:
    """path + '?' + query，问号本身也算一个 octet。"""
    assert request_target_bytes({"raw_path": b"/healthz", "query_string": b"a=1"}) == 12


def test_target_bytes_counts_raw_undecoded_octets() -> None:
    """必须按未 percent-decode 的原始字节计数：%E4%B8%AD 是 9 bytes 不是 1 个字符。"""
    assert request_target_bytes({"raw_path": b"/a/%E4%B8%AD", "query_string": b""}) == 12


def test_target_bytes_falls_back_to_path_when_no_raw_path() -> None:
    """ASGI server 不提供 raw_path 时回退已解码 path 的 UTF-8 编码。"""
    assert request_target_bytes({"path": "/中", "query_string": b""}) == 4


# ── 中间件行为：关联 id ────────────────────────────────────────────────────────


def test_hostile_trace_id_not_reflected_in_header_or_body(app: FastAPI) -> None:
    """注入串不得进响应头与响应体（此前 `evil<script>"; DROP--` 两处都进了）。"""
    r = TestClient(app).get("/healthz", headers={"X-Trace-Id": HOSTILE_ID})
    assert r.status_code == 200
    assert r.headers["x-trace-id"].startswith("trc-")
    assert HOSTILE_ID not in r.headers["x-trace-id"]
    assert HOSTILE_ID not in r.text
    assert r.json()["trace_id"] == r.headers["x-trace-id"]


@pytest.mark.parametrize(
    "raw",
    ["z" * 300, "b" * 100, HOSTILE_ID, "req/2026/06/11/abc", "req+abc=123"],
    ids=["overlong-300", "legal-100", "hostile", "slashes", "plus-equals"],
)
def test_business_request_id_is_verbatim_but_echo_is_controlled(app: FastAPI, raw: str) -> None:
    """业务 `request_id` 逐字保留原值；回显 `X-Request-Id` 用受控值。两个角色不互相顶替。

    这是本模块最关键的一条护栏。`request_id` 是三个唯一约束
    （uq_inbox_tenant_src_req / uq_recon_task_req / uq_wedap_delivery_request）的幂等键——
    一旦在中间件被重签成随机 uuid，唯一约束**永不命中**，同一笔银行回调会被摄取两次
    （静默重复处理，比撞 DB 约束的 500 严重得多）。
    同时 §11.1 要求不可信值不得原样回显，所以响应头必须是过校验/重签的受控值。
    """
    from app.core.context import current_ids

    seen: list[tuple[str | None, str]] = []

    @app.get("/test-req-id-probe")
    async def _probe() -> dict[str, str]:
        ids = current_ids()
        seen.append((ids.request_id, ids.safe_request_id))
        return {}

    r = TestClient(app).get(
        "/test-req-id-probe",
        headers={"X-Caller-Service": "test", "X-Request-Id": raw},
    )
    assert r.status_code == 200
    business, echoed = seen[0]
    # 业务侧：原值一个字符都不能改（幂等键语义）
    assert business == raw
    # 回显侧：受控、不泄漏原值、不超上限
    assert echoed.startswith("req-") and len(echoed) <= CORRELATION_ID_MAX_LEN
    assert raw not in echoed
    assert r.headers["x-request-id"] == echoed
    assert raw not in r.headers["x-request-id"]


def test_valid_request_id_echoed(app: FastAPI) -> None:
    r = TestClient(app).get("/healthz", headers={"X-Request-Id": "req-echo-1"})
    assert r.headers["x-request-id"] == "req-echo-1"


def test_response_always_carries_request_id(app: FastAPI) -> None:
    """§7.4：调用方没传也要回一个受控 X-Request-Id（报障时唯一可对齐日志的标识）。"""
    r = TestClient(app).get("/healthz")
    assert r.headers["x-request-id"].startswith("req-")


def _mount_require_headers_probe(app: FastAPI) -> TestClient:
    """挂一条走真实 require_headers 依赖的探针路由。"""
    from fastapi import Depends

    from app.api.deps import require_headers

    _headers_dep = Depends(require_headers)

    @app.get("/test-require-headers-probe")
    async def _probe(hdr: dict[str, str] = _headers_dep) -> dict[str, str]:
        return hdr

    return TestClient(app)


@pytest.mark.parametrize(
    ("label", "extra_headers"),
    [
        ("absent", {}),
        ("empty", {"X-Request-Id": ""}),
    ],
)
def test_absent_or_empty_request_id_still_400(
    app: FastAPI, label: str, extra_headers: dict[str, str]
) -> None:
    """回归护栏：重签不得把「没传」或「传了空值」掩盖成合法请求。

    走真实的 require_headers 依赖。两种输入在 origin/main 都是 400 GW_400_HEADER
    （`request.headers.get()` 分别返回 None 与 ""，双双 falsy）。本波的重签逻辑
    一旦把它们补成受控值，这条既有 400 就静默变 200 —— 属于改状态码取值（本波严禁），
    且会让调用方 SDK 漏填 request id 时由网关代签随机值写进去重列。

    `empty` 这条是端点级覆盖：只测 sanitize_correlation_id 的单元用例拦不住它，
    因为空值能否 400 取决于中间件把哪个值放进 `RequestIds.request_id`。
    """
    client = _mount_require_headers_probe(app)
    r = client.get(
        "/test-require-headers-probe",
        headers={"X-Caller-Service": "test", "X-Tenant-Id": "WBTHK01", **extra_headers},
    )
    assert r.status_code == 400, f"{label}: 期望 400，实际 {r.status_code}"
    assert r.json()["error"]["code"] == "GW_400_HEADER"
    assert "X-Request-Id" in r.json()["error"]["message"]


def test_valid_request_id_passes_require_headers(app: FastAPI) -> None:
    """对照组：带合规值就通过，证明上面的 400 来自「缺失/空值」而非新校验误伤。"""
    client = _mount_require_headers_probe(app)
    ok = client.get(
        "/test-require-headers-probe",
        headers={
            "X-Caller-Service": "test",
            "X-Tenant-Id": "WBTHK01",
            "X-Request-Id": "req-ok-1",
        },
    )
    assert ok.status_code == 200 and ok.json()["request_id"] == "req-ok-1"


def test_long_legal_request_id_reaches_handler_verbatim(app: FastAPI) -> None:
    """65–128 字符的合法 request_id 必须原样到达 handler，不得被重签或截断。

    callback_inbox.request_id / recon_result_task.request_id 都是 String(128)，
    DDL 明确允许这个区间。若中间件按 64 收窄并重签，这类值会：
    ① 打掉去重（唯一约束永不命中）② 让 recon_notify 的 recon-result-* 解析失败变 400。
    """
    client = _mount_require_headers_probe(app)
    long_id = "b" * 100
    ok = client.get(
        "/test-require-headers-probe",
        headers={
            "X-Caller-Service": "test",
            "X-Tenant-Id": "WBTHK01",
            "X-Request-Id": long_id,
        },
    )
    assert ok.status_code == 200
    assert ok.json()["request_id"] == long_id


# ── 中间件行为：Cache-Control ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "headers", "expected_status"),
    [
        ("/healthz", {}, 200),
        ("/api/version", {}, 200),
        ("/api/v1/admin/stuck-orders", {}, 401),
        ("/no-such-route", {"X-Caller-Service": "test"}, 404),
    ],
)
def test_no_store_on_every_response(
    app: FastAPI, path: str, headers: dict[str, str], expected_status: int
) -> None:
    """API-HTTP-015：成功读、发布身份、鉴权失败、路由 miss 一律 no-store。"""
    r = TestClient(app).get(path, headers=headers)
    assert r.status_code == expected_status
    assert r.headers["cache-control"] == "no-store"


def test_no_store_on_500(app: FastAPI) -> None:
    """500 由 ServerErrorMiddleware 处理，不回经 IdentifierMiddleware——也必须 no-store。"""

    @app.get("/test-boom-no-store")
    async def _boom() -> dict[str, str]:
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/test-boom-no-store", headers={"X-Caller-Service": "test"})
    assert r.status_code == 500
    assert r.headers["cache-control"] == "no-store"


def test_500_carries_correlation_id_headers(app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
    """§7.4「所有响应」+ §11.1：500 也必须带受控 X-Request-Id / X-Trace-Id。

    500 挂在 ServerErrorMiddleware 上（在所有用户中间件之外），响应不回经
    IdentifierMiddleware，两个头必须由 `_generic_exception_handler` 自己补。
    未处理异常恰恰是调用方**一定会来报障**的那一类响应——它若是全站唯一拿不到
    关联 id 的响应，「X-Request-Id 全响应回传」这句自述就不成立，报障无从对齐日志。

    同时校验：调用方传入的畸形值不得从这条路径原样回显（§11.1 绕过口）。
    """

    @app.get("/test-boom-ids")
    async def _boom() -> dict[str, str]:
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)

    # (a) 传了合规值 → 原样回显，调用方能用它对日志
    with caplog.at_level(logging.ERROR, logger="app.main"):
        r = client.get(
            "/test-boom-ids",
            headers={
                "X-Caller-Service": "test",
                "X-Request-Id": "req-boom-1",
                "X-Trace-Id": "trc-boom-1",
            },
        )
    assert r.status_code == 500
    assert r.headers["x-request-id"] == "req-boom-1"
    assert r.headers["x-trace-id"] == "trc-boom-1"
    # 回链闭环：响应头上的两个 id 必须同时出现在服务端那条 500 日志里，
    # 否则调用方拿着响应头来报障，服务端 grep 不到对应行。
    boom_log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "req-boom-1" in boom_log, f"500 日志缺 request_id: {boom_log}"
    assert "trc-boom-1" in boom_log, f"500 日志缺 trace_id: {boom_log}"

    # (b) 完全没传 → 仍要各回一个受控值，不能缺头
    r2 = client.get("/test-boom-ids", headers={"X-Caller-Service": "test"})
    assert r2.status_code == 500
    assert r2.headers["x-request-id"].startswith("req-")
    assert r2.headers["x-trace-id"].startswith("trc-")

    # (c) 传了注入串 → 重签，一个字符都不许回显
    r3 = client.get(
        "/test-boom-ids",
        headers={
            "X-Caller-Service": "test",
            "X-Request-Id": HOSTILE_ID,
            "X-Trace-Id": HOSTILE_ID,
        },
    )
    assert r3.status_code == 500
    assert HOSTILE_ID not in r3.headers["x-request-id"]
    assert HOSTILE_ID not in r3.headers["x-trace-id"]
    assert HOSTILE_ID not in r3.text


# ── 中间件行为：8,192-byte 预算 → 414 ─────────────────────────────────────────


def _query_of_target_size(path: str, total: int) -> str:
    """构造使 raw request-target 恰好为 total 字节的 query（含 '?' 与 'x=' 前缀）。"""
    return "x=" + "a" * (total - len(path) - len("?x="))


def test_target_at_budget_passes(app: FastAPI) -> None:
    """恰好 8,192 bytes 仍按合同处理（边界不能宽一格也不能严一格）。"""
    query = _query_of_target_size("/healthz", MAX_REQUEST_TARGET_BYTES)
    r = TestClient(app).get(f"/healthz?{query}")
    assert r.status_code == 200


def test_target_over_budget_returns_414(app: FastAPI) -> None:
    """8,193 bytes → 414（不得降成 400/413/422）。"""
    query = _query_of_target_size("/healthz", MAX_REQUEST_TARGET_BYTES + 1)
    r = TestClient(app).get(f"/healthz?{query}")
    assert r.status_code == 414
    assert r.json()["error"]["code"] == "GW_414_URI_TOO_LONG"
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["trace_id"] == r.headers["x-trace-id"]


def test_414_precedes_authentication(app: FastAPI) -> None:
    """§7.2.1 第 1 步：预算裁决先于认证——未带 S2S 头的超长请求也必须 414 而不是 401。"""
    query = _query_of_target_size("/api/v1/admin/stuck-orders", MAX_REQUEST_TARGET_BYTES + 100)
    r = TestClient(app).get(f"/api/v1/admin/stuck-orders?{query}")
    assert r.status_code == 414


def test_414_precedes_business_handler(app: FastAPI) -> None:
    """§7.2.1 第 6 条：拒绝必须发生在业务数据访问之前——handler 一次都不能被调到。"""
    called: list[int] = []

    @app.get("/test-budget-probe")
    async def _probe() -> dict[str, str]:
        called.append(1)
        return {}

    query = _query_of_target_size("/test-budget-probe", MAX_REQUEST_TARGET_BYTES + 1)
    r = TestClient(app).get(f"/test-budget-probe?{query}", headers={"X-Caller-Service": "test"})
    assert r.status_code == 414
    assert called == []
