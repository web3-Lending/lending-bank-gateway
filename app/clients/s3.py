"""S3 文件下载客户端：同步 boto3，调用方在 asyncio.to_thread 中执行（worker 上下文）。"""

import hashlib
import pathlib

import boto3  # type: ignore[import-untyped]
import httpx
from botocore.config import Config  # type: ignore[import-untyped]


class Md5Mismatch(Exception):
    """下载内容的 md5 与契约期望值不符。"""


class S3FileClient:
    """同步 S3 下载封装——调用方在 asyncio.to_thread 中执行（worker 上下文）。

    显式 connect/read 超时 + 重试上限（FU-WEDAP-S3-TIMEOUT）：远端 S3 卡死时不再
    无限占用 worker 线程拖住 dispatcher 轮转。
    """

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        max_attempts: int = 3,
    ) -> None:
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            config=Config(
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                retries={"max_attempts": max_attempts, "mode": "standard"},
            ),
        )
        # presigned URL 走 HTTP，不经 boto3；复用 connect/read 超时避免卡死 worker。
        self._http_timeout = httpx.Timeout(
            connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout
        )

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

    def upload(self, *, bucket: str, key: str, content: bytes) -> str:
        """上传字节到 S3（wedap flow-import 导入文件），返回内容 SHA-256 小写 hex。

        wedap fileChecksum 用 SHA-256（spec §4，区别于下载侧 md5 契约）；调用方可用
        返回值与生成侧 BatchFile.checksum 比对，确保上传内容一致。
        """
        self._s3.put_object(Bucket=bucket, Key=key, Body=content)
        return hashlib.sha256(content).hexdigest()

    def get_bytes(self, *, bucket: str, key: str) -> bytes:
        """读取 S3 对象字节（recon→gateway staging 取数；dispatcher 用）。"""
        return self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()  # type: ignore[no-any-return]

    # ---- ADR-0001 P4：presigned URL 上传/下载（lending 无需长期 wedap S3 凭证）----

    def upload_via_presigned_put(self, *, url: str, content: bytes) -> str:
        """经 wedap 颁发的 presigned PUT URL 上传导入文件（HTTP PUT，无需 S3 凭证）。

        返回内容 SHA-256 小写 hex（同 upload()，供与生成侧 checksum 比对）。非 2xx 抛
        httpx.HTTPStatusError（URL 过期/越权/不匹配签名前缀由 S3 判定）。
        """
        resp = httpx.put(url, content=content, timeout=self._http_timeout)
        resp.raise_for_status()
        return hashlib.sha256(content).hexdigest()

    def get_bytes_via_presigned_get(self, *, url: str) -> bytes:
        """经 wedap 颁发的 presigned GET URL 读取 result 文件（HTTP GET，无需 S3 凭证）。"""
        resp = httpx.get(url, timeout=self._http_timeout)
        resp.raise_for_status()
        return resp.content
