"""wedap flow-import 结果回收：解析 wedap 写回 S3 的结果文件（spec §7）。

结果文件路径：``{bucket}/lending/result/{dataType}/{channelId}/{importDate}/{importBatchNo}_result.json``
内容关注：importStatus(SUCCESS/PARTIAL/FAILED) + ingested/duplicate/lineError 计数 +
lineResults[]（逐行 lineStatus + errorMessage）。PARTIAL/FAILED 时据 lineResults 定位
坏行，修正后走修复重传（新 importBatchNo + replacesBatchNo）。

本模块只做「key 构建」与「字节 → 结构化」的纯逻辑；S3 拉取由调用方负责。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

CHANNEL_ID = "LEN"

STATUS_SUCCESS = "SUCCESS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAILED = "FAILED"

# web2-core ErrorCode 枚举（9 值，与 line_status 正交）——仅作契约认知/观测参考；
# lending 原样透传 errorCode 不校验枚举（未来 wedap 加值不阻塞回收）。ADR-0001 P5。
KNOWN_LINE_ERROR_CODE = frozenset(
    {
        "INVALID_JSON",
        "DEDUP_KEY_MISSING",
        "REQUIRED_FIELD_MISSING",
        "INVALID_NUMBER",
        "INVALID_DATE_FORMAT",
        "INVALID_ENUM",
        "UNSUPPORTED_SCHEMA_VERSION",
        "CONTRACT_SHAPE_MISMATCH",
        "UNKNOWN_PARSE_ERROR",
    }
)


@dataclass(frozen=True)
class BadLine:
    """一条非 INGESTED 的结果行（DUPLICATE / LINE_PARSE_ERROR / CONTRACT_INVALID）。"""

    line_no: int | None
    line_status: str
    error_message: str | None
    # wedap LineResult.errorCode（web2-core ErrorCode **9 值**枚举，见 KNOWN_LINE_ERROR_CODE）；
    # DLQ 按它分类，成功/DUPLICATE 行 null。lending 原样透传（截 recon max_length=64）、不校验枚举。
    error_code: str | None = None
    # 结构化去重键；LINE_PARSE_ERROR 行 null（失败行回映靠 lineNo+manifest）。
    dedup_key: dict[str, str] | None = None


@dataclass(frozen=True)
class ImportResult:
    """解析后的批次导入结果。"""

    import_status: str
    ingested_count: int
    duplicate_count: int
    line_error_count: int
    bad_lines: list[BadLine] = field(default_factory=list)

    @property
    def is_terminal_ok(self) -> bool:
        """全部成功（无需修复重传）。"""
        return self.import_status == STATUS_SUCCESS

    @property
    def needs_repair(self) -> bool:
        """PARTIAL/FAILED 且存在解析错误行 → 需修复重传。"""
        return self.import_status in (STATUS_PARTIAL, STATUS_FAILED)


def build_result_key(*, data_type: str, import_date: str, import_batch_no: str) -> str:
    """`lending/result/{dataType}/LEN/{importDate}/{importBatchNo}_result.json`（spec §7）。"""
    return f"lending/result/{data_type}/{CHANNEL_ID}/{import_date}/{import_batch_no}_result.json"


def parse_result(raw: bytes) -> ImportResult:
    """结果文件字节 → ImportResult；只收集非 INGESTED 行进 bad_lines。"""
    body: dict[str, Any] = json.loads(raw)
    bad_lines = [
        BadLine(
            line_no=item.get("lineNo"),
            line_status=str(item.get("lineStatus")),
            error_message=item.get("errorMessage"),
            error_code=item.get("errorCode"),
            dedup_key=item.get("dedupKey"),
        )
        for item in (body.get("lineResults") or [])
        if str(item.get("lineStatus")) != "INGESTED"
    ]
    return ImportResult(
        import_status=str(body.get("importStatus")),
        ingested_count=int(body.get("ingestedCount") or 0),
        duplicate_count=int(body.get("duplicateCount") or 0),
        line_error_count=int(body.get("lineErrorCount") or 0),
        bad_lines=bad_lines,
    )
