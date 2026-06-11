"""S3FileClient 单元测试：使用 botocore Stubber 隔离真实 S3 调用。"""

import hashlib
import io

import pytest
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
