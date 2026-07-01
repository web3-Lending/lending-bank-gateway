"""S3FileClient 单元测试：使用 botocore Stubber 隔离真实 S3 调用。"""

import hashlib
import io

import httpx
import pytest
import respx
from botocore.stub import Stubber

from app.clients.s3 import Md5Mismatch, S3FileClient

CONTENT = b"excel-bytes"
MD5 = hashlib.md5(CONTENT, usedforsecurity=False).hexdigest()


def _client_with_stub(body: bytes) -> S3FileClient:
    c = S3FileClient(endpoint_url=None)
    stub = Stubber(c._s3)
    stub.add_response("get_object", {"Body": io.BytesIO(body)}, {"Bucket": "b", "Key": "k"})
    stub.activate()
    return c


def test_download_and_verify_ok(tmp_path) -> None:  # type: ignore[no-untyped-def]
    c = _client_with_stub(CONTENT)
    dest = tmp_path / "f.xlsx"
    c.download_verified(bucket="b", key="k", expected_md5=MD5, dest=str(dest))
    assert dest.read_bytes() == CONTENT


def test_md5_mismatch_raises_and_no_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    c = _client_with_stub(CONTENT)
    dest = tmp_path / "f.xlsx"
    with pytest.raises(Md5Mismatch):
        c.download_verified(bucket="b", key="k", expected_md5="0" * 32, dest=str(dest))
    assert not dest.exists()  # 校验失败不落地


def test_expected_md5_case_insensitive(tmp_path) -> None:  # type: ignore[no-untyped-def]
    c = _client_with_stub(CONTENT)
    c.download_verified(
        bucket="b", key="k", expected_md5=MD5.upper(), dest=str(tmp_path / "f.xlsx")
    )


def test_upload_puts_object_and_returns_sha256() -> None:  # type: ignore[no-untyped-def]
    c = S3FileClient(endpoint_url=None)
    stub = Stubber(c._s3)
    key = "lending/import/interest-accrual/LEN/20260624/BATCH-LEN-20260624-001.jsonl"
    stub.add_response("put_object", {}, {"Bucket": "wedap-bucket", "Key": key, "Body": CONTENT})
    stub.activate()

    checksum = c.upload(bucket="wedap-bucket", key=key, content=CONTENT)

    stub.assert_no_pending_responses()
    assert checksum == hashlib.sha256(CONTENT).hexdigest()
    assert len(checksum) == 64


def test_get_bytes_returns_object_body() -> None:  # type: ignore[no-untyped-def]
    c = _client_with_stub(b"staging-jsonl-bytes")
    assert c.get_bytes(bucket="b", key="k") == b"staging-jsonl-bytes"


def test_upload_returns_sha256() -> None:  # type: ignore[no-untyped-def]
    import hashlib as _h

    c = S3FileClient(endpoint_url=None)
    stub = Stubber(c._s3)
    stub.add_response("put_object", {}, {"Bucket": "b", "Key": "k", "Body": b"x"})
    stub.activate()
    assert c.upload(bucket="b", key="k", content=b"x") == _h.sha256(b"x").hexdigest()


def test_client_has_explicit_timeouts_and_retries() -> None:  # type: ignore[no-untyped-def]
    c = S3FileClient(endpoint_url=None, connect_timeout=3.0, read_timeout=7.0, max_attempts=4)
    cfg = c._s3.meta.config
    assert cfg.connect_timeout == 3.0
    assert cfg.read_timeout == 7.0
    # botocore standard 模式把 max_attempts=4 规范成 total_max_attempts=5（+1 初次）
    assert cfg.retries["total_max_attempts"] == 5
    assert cfg.retries["mode"] == "standard"


# ---- ADR-0001 P4：presigned URL 上传/下载（HTTP，无需 S3 凭证）----

_PUT_URL = "https://s3.example/lending/import/loan-detail/LEN/20260701/B1.jsonl?X-Amz-Signature=abc"
_GET_URL = (
    "https://s3.example/lending/result/loan-detail/LEN/20260701/B1_result.json?X-Amz-Signature=xyz"
)


@respx.mock
def test_upload_via_presigned_put_ok() -> None:
    route = respx.put(_PUT_URL).mock(return_value=httpx.Response(200))
    sha = S3FileClient(endpoint_url=None).upload_via_presigned_put(url=_PUT_URL, content=CONTENT)
    assert sha == hashlib.sha256(CONTENT).hexdigest()
    req = route.calls.last.request
    assert req.content == CONTENT  # 字节原样 PUT
    # presigned URL 约束：不得加 Authorization / 显式 Content-Type，否则破坏 S3 签名
    assert "authorization" not in {k.lower() for k in req.headers}
    assert "content-type" not in {k.lower() for k in req.headers}


@respx.mock
def test_upload_via_presigned_put_non_2xx_raises() -> None:
    respx.put(_PUT_URL).mock(return_value=httpx.Response(403, text="expired"))
    with pytest.raises(httpx.HTTPStatusError):
        S3FileClient(endpoint_url=None).upload_via_presigned_put(url=_PUT_URL, content=CONTENT)


@respx.mock
def test_get_bytes_via_presigned_get_ok() -> None:
    respx.get(_GET_URL).mock(return_value=httpx.Response(200, content=b"result-json-bytes"))
    body = S3FileClient(endpoint_url=None).get_bytes_via_presigned_get(url=_GET_URL)
    assert body == b"result-json-bytes"


@respx.mock
def test_get_bytes_via_presigned_get_non_2xx_raises() -> None:
    respx.get(_GET_URL).mock(return_value=httpx.Response(404, text="not found"))
    with pytest.raises(httpx.HTTPStatusError):
        S3FileClient(endpoint_url=None).get_bytes_via_presigned_get(url=_GET_URL)
