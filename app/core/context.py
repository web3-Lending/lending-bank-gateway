"""请求关联标识（trace/request id）+ 入口级 HTTP 预算与缓存策略。

本模块是三条 v2.2 规则在本仓的唯一收口点，全部先于业务 handler 生效：

- **API-HTTP-013 + §7.4/§11.1**：调用方传入的 `X-Trace-Id` / `X-Request-Id` 是不可信
  输入，必须过长度与字符集校验；不合规的值不得复用，重签为受控值。所有响应回受控
  `X-Trace-Id` 与 `X-Request-Id`。
- **API-HTTP-015 + §7.4**：本服务全部响应默认 `Cache-Control: no-store`。
- **API-HTTP-003/006 + §7.2.1 第 1 步**：raw request-target 8,192-byte 硬预算，超限在
  percent-decode、认证与路由裁决之前固定 `414`。
"""

import re
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Scope

from app.core.envelope import err


@dataclass(frozen=True)
class RequestIds:
    """一次请求的关联标识。

    `request_id` 与 `safe_request_id` 是**两个角色**，不能互相顶替：

    - `request_id`：调用方 `X-Request-Id` 的**原值**（header 没传 → None）。它是业务
      幂等键——`callback_inbox.uq_inbox_tenant_src_req`、`recon_result_task.uq_recon_task_req`、
      `wedap_import_delivery_task.uq_wedap_delivery_request` 三个唯一约束都拿它去重，
      `recon_notify._parse_request_id` 还要按 `recon-result-{taskNo}-v{n}` 解析它。
      **一旦重签成随机值，唯一约束永不命中 → 同一笔银行回调被摄取两次**，所以这里
      必须逐字保留调用方原值，与 origin/main 语义一致。
    - `safe_request_id`：过校验/重签后的**受控回显值**，只用于响应头与结构化日志
      （§7.4「所有响应必须返回受控 X-Request-Id」+ §11.1）。它**永远不得**当业务键使用。
    """

    trace_id: str
    request_id: str | None
    tenant_id: str | None
    biz_seq_no: str | None
    safe_request_id: str = "req-none"


_DEFAULT_IDS = RequestIds("trc-none", None, None, None)
_ids: ContextVar[RequestIds] = ContextVar("ids", default=_DEFAULT_IDS)

# 关联 id 的受控形状（API-HTTP-013 / §11.1）。本上限约束的是**受控回显值**——
# `trace_id`（落 query_audit.trace_id / callback_outbox.trace_id，均 String(64)）与
# `safe_request_id`（只进响应头与日志，不落库）。取 64 = 上述落库列宽的最小值，
# 保证通过校验的 trace_id 不会在任何一张表上被截断或撞约束变 GW_500_INTERNAL。
#
# **业务 `request_id` 不受本上限约束**：它是幂等键，必须逐字保留调用方原值
# （见 `RequestIds` 字段说明）。它的列宽由各表自己决定（callback_inbox 128 /
# recon_result_task 128 / wedap_import_delivery_task 96 / bank_txn_order 64）。
CORRELATION_ID_MAX_LEN = 64
_CORRELATION_ID_RE = re.compile(rf"[A-Za-z0-9._:-]{{1,{CORRELATION_ID_MAX_LEN}}}")


def sanitize_correlation_id(raw: str | None, *, prefix: str) -> str | None:
    """把调用方传入的关联 id 归一为**可回显**的受控值。

    返回值只用于「回显」角色（响应头 / 结构化日志 / trace_id），**绝不能拿来当业务
    幂等键**——重签会让唯一约束永不命中，详见 `RequestIds` 的字段说明。

    - `None`（没传）或纯空白 → 返回 `None`，交由调用方 `or` 出一个新签值。
      空白等同「没传」是 HTTP 语义（RFC 9110 §5.5 field value 前后空白无意义），
      也让 `X-Request-Id: ` 与「根本没传」在回显侧收敛到同一处理。
    - 通过校验 → 原值可信，直接复用（跨服务链路靠它对得上）。
    - 未通过（超长 / 含控制字符、引号、尖括号、空格等）→ 重签 `<prefix>-<uuid4hex>`：
      调用方不能用畸形值伪造内部审计身份，也不能把脚本片段或超长串送进响应头、
      结构化日志与 DB 列。
    """
    if raw is None or not raw.strip():
        return None
    if _CORRELATION_ID_RE.fullmatch(raw):
        return raw
    return f"{prefix}-{uuid.uuid4().hex}"


def apply_no_store(response: Response) -> None:
    """给响应打 `Cache-Control: no-store`（API-HTTP-015 + §7.4）。

    本网关**没有**「公开可缓存 GET」，因此不设例外白名单：28 个 operation 要么是账户、
    交易、鉴权这类敏感读与资金写结果（规范默认 no-store），要么是 `/api/version`、
    `/build-info`、`/healthz`、`/readyz` 这类发布身份与健康探针——后者一旦被中间层缓存，
    部署验证会读到旧镜像身份、健康检查会读到旧状态，比不缓存更危险。
    """
    response.headers["Cache-Control"] = "no-store"


def current_ids() -> RequestIds:
    return _ids.get()


class IdentifierMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        raw_request_id = request.headers.get("X-Request-Id")
        ids = RequestIds(
            trace_id=sanitize_correlation_id(request.headers.get("X-Trace-Id"), prefix="trc")
            or f"trc-{uuid.uuid4().hex}",
            # 业务/幂等键：逐字保留调用方原值，与 origin/main 一致。这里**不能**做
            # sanitize——重签会打掉 uq_inbox_tenant_src_req / uq_recon_task_req 去重
            # （同一笔回调摄取两次），也会让 recon_notify 的 recon-result-* 解析失败。
            request_id=raw_request_id,
            tenant_id=request.headers.get("X-Tenant-Id"),
            biz_seq_no=request.headers.get("X-Biz-Seq-No"),
            # 回显值：过校验/重签，只进响应头与日志。
            safe_request_id=sanitize_correlation_id(raw_request_id, prefix="req")
            or f"req-{uuid.uuid4().hex}",
        )
        token = _ids.set(ids)
        try:
            response = await call_next(request)
        finally:
            _ids.reset(token)
        response.headers["X-Trace-Id"] = ids.trace_id
        # §7.4「所有响应必须返回受控 X-Request-Id」：调用方没传时也要回一个——报障时
        # 它是把这次响应与网关日志对上的唯一标识（同值进 _JsonLogFormatter 的
        # request_id 字段，服务端可直接 grep 命中）。回显受控值不影响 ids.request_id：
        # 后者为 None/"" 时 require_headers 照常返 400，不被本行掩盖。
        response.headers["X-Request-Id"] = ids.safe_request_id
        apply_no_store(response)
        return response


# §7.2.1 第 1 步 / API-HTTP-003 / API-HTTP-006：LENDING_QUERY_V1 组级硬预算。
# <= 8,192 bytes 继续按合同处理；>= 8,193 bytes 固定 414（不得改用 400/413/422）。
MAX_REQUEST_TARGET_BYTES = 8192


def request_target_bytes(scope: Scope) -> int:
    """按 octet 统计原始 request-target（`path` + `?` + `query`）长度。

    优先用 ASGI `raw_path`（未 percent-decode 的原始字节，uvicorn 与 TestClient 均提供）；
    个别不提供 `raw_path` 的 ASGI server 回退到已解码 path 的 UTF-8 编码——回退值只会
    偏小不会偏大，不会把预算内的合规请求误判超限。
    """
    raw_path = bytes(scope.get("raw_path") or scope["path"].encode("utf-8"))
    query = bytes(scope.get("query_string", b""))
    return len(raw_path) + (len(query) + 1 if query else 0)


class RequestTargetLimitMiddleware(BaseHTTPMiddleware):
    """raw request-target 超 8,192 bytes → 414，先于认证、路由裁决与业务解析。

    §7.2.1 第 3 条要求「超过 8,192 bytes 时无条件返回 414，即使其内容同时不可解析」，
    所以本中间件必须装在 S2S 之外：认证失败的 401 也不能抢在预算裁决之前。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request_target_bytes(request.scope) > MAX_REQUEST_TARGET_BYTES:
            return JSONResponse(
                err(
                    "GW_414_URI_TOO_LONG",
                    "request-target exceeds the 8192-byte budget",
                    trace_id=current_ids().trace_id,
                ),
                status_code=414,
            )
        return await call_next(request)
