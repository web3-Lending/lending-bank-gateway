"""wedap flow-import 结果解析单测。"""

import json

from app.services.wedap_import_result import (
    build_result_key,
    parse_result,
)


def test_build_result_key():
    key = build_result_key(
        data_type="interest-accrual",
        import_date="20260624",
        import_batch_no="BATCH-LEN-20260624-001",
    )
    assert key == ("wedap/result/interest-accrual/LEN/20260624/BATCH-LEN-20260624-001_result.json")


def test_parse_success_no_bad_lines():
    raw = json.dumps(
        {
            "importStatus": "SUCCESS",
            "ingestedCount": 10,
            "duplicateCount": 0,
            "lineErrorCount": 0,
            "lineResults": [
                {"lineNo": 2, "lineStatus": "INGESTED"},
                {"lineNo": 3, "lineStatus": "INGESTED"},
            ],
        }
    ).encode()
    result = parse_result(raw)

    assert result.import_status == "SUCCESS"
    assert result.ingested_count == 10
    assert result.bad_lines == []
    assert result.is_terminal_ok is True
    assert result.needs_repair is False


def test_parse_partial_collects_only_non_ingested():
    raw = json.dumps(
        {
            "importStatus": "PARTIAL",
            "ingestedCount": 8,
            "duplicateCount": 1,
            "lineErrorCount": 1,
            "lineResults": [
                {"lineNo": 2, "lineStatus": "INGESTED"},
                {"lineNo": 3, "lineStatus": "DUPLICATE"},
                {"lineNo": 4, "lineStatus": "LINE_PARSE_ERROR", "errorMessage": "bad json"},
            ],
        }
    ).encode()
    result = parse_result(raw)

    assert result.import_status == "PARTIAL"
    assert result.needs_repair is True
    assert result.is_terminal_ok is False
    assert [b.line_status for b in result.bad_lines] == ["DUPLICATE", "LINE_PARSE_ERROR"]
    assert result.bad_lines[1].line_no == 4
    assert result.bad_lines[1].error_message == "bad json"


def test_parse_reads_error_code_and_dedup_key():
    """errorCode(8 值枚举)+ 结构化 dedupKey 须解析进 BadLine（DLQ 分类/回映用）。"""
    raw = json.dumps(
        {
            "importStatus": "PARTIAL",
            "ingestedCount": 1,
            "duplicateCount": 1,
            "lineErrorCount": 1,
            "lineResults": [
                {"lineNo": 2, "lineStatus": "INGESTED"},
                {
                    "lineNo": 3,
                    "lineStatus": "DUPLICATE",
                    "dedupKey": {"loanId": "LOAN-1", "asOfDate": "20260630"},
                },
                {
                    "lineNo": 4,
                    "lineStatus": "LINE_PARSE_ERROR",
                    "errorCode": "REQUIRED_FIELD_MISSING",
                    "errorMessage": "missing principal",
                },
            ],
        }
    ).encode()
    result = parse_result(raw)
    dup, err = result.bad_lines
    assert dup.line_status == "DUPLICATE"
    assert dup.dedup_key == {"loanId": "LOAN-1", "asOfDate": "20260630"}
    assert err.line_status == "LINE_PARSE_ERROR"
    assert err.error_code == "REQUIRED_FIELD_MISSING"
    assert err.dedup_key is None  # 失败行 dedupKey 为 null，回映靠 lineNo+manifest


def test_parse_failed_status():
    raw = json.dumps({"importStatus": "FAILED", "ingestedCount": 0, "lineErrorCount": 5}).encode()
    result = parse_result(raw)
    assert result.import_status == "FAILED"
    assert result.needs_repair is True
    assert result.duplicate_count == 0  # 缺省补 0


def test_parse_missing_line_results_yields_empty_bad_lines():
    raw = json.dumps({"importStatus": "SUCCESS", "ingestedCount": 3}).encode()
    result = parse_result(raw)
    assert result.bad_lines == []
    assert result.line_error_count == 0
