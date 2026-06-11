from app.core.envelope import err, ok


def test_ok_envelope_shape() -> None:
    body = ok({"a": 1}, trace_id="trc-1")
    assert body == {"success": True, "data": {"a": 1}, "error": None, "trace_id": "trc-1"}


def test_err_envelope_shape() -> None:
    body = err("GW_400_VALIDATION", "bad biz_seq_no", trace_id="trc-2", details={"f": "bizSeqNo"})
    assert body["success"] is False and body["data"] is None
    assert body["error"] == {
        "code": "GW_400_VALIDATION",
        "message": "bad biz_seq_no",
        "details": {"f": "bizSeqNo"},
    }
    assert body["trace_id"] == "trc-2"
