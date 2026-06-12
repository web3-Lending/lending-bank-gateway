"""S3 文件下载客户端：同步 boto3，调用方在 asyncio.to_thread 中执行（worker 上下文）。"""

import hashlib
import pathlib

import boto3  # type: ignore[import-untyped]


class Md5Mismatch(Exception):
    """下载内容的 md5 与契约期望值不符。"""


class S3FileClient:
    """同步 S3 下载封装——调用方在 asyncio.to_thread 中执行（worker 上下文）。"""

    def __init__(self, *, endpoint_url: str | None) -> None:
        self._s3 = boto3.client("s3", endpoint_url=endpoint_url)

    def download_verified(self, *, bucket: str, key: str, expected_md5: str, dest: str) -> None:
        """下载 → md5 校验 → 校验通过才落地存档；不符抛 Md5Mismatch 不写文件。

        注：md5 用于契约完整性校验（非安全哈希用途）。
        """
        body = self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        actual = hashlib.md5(body, usedforsecurity=False).hexdigest()  # noqa: S324
        if actual != expected_md5.lower():
            raise Md5Mismatch(f"{actual} != {expected_md5.lower()}")
        path = pathlib.Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
