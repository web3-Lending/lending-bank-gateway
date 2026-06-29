"""recon→gateway wedap flow-import 投递交单端点（§4.3「A+异步回执」）。

recon 生成并落 staging 后调本端点交单（只传元数据，不传文件字节）。gateway 落一条
PENDING 投递任务（幂等），dispatcher 异步取 staging 字节投递。X-Request-Id =
wedap-import-{importBatchNo}（服务间幂等键）。
"""

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import require_headers
from app.services.wedap_delivery import enqueue_and_serialize

router = APIRouter(prefix="/api/v1/wedap/import", tags=["wedap-import"])


class EnqueueRequest(BaseModel):
    import_batch_no: str = Field(min_length=1, max_length=64)
    data_type: str = Field(min_length=1, max_length=32)
    import_date: str = Field(min_length=8, max_length=8)  # YYYYMMDD
    staging_key: str = Field(min_length=1, max_length=512)
    file_checksum: str = Field(min_length=64, max_length=64)  # SHA-256 hex
    file_size: int = Field(ge=0)
    total_count: int = Field(ge=0)


@router.post("/enqueue")
async def enqueue_import(request: Request, body: EnqueueRequest) -> dict[str, Any]:
    """交单一条投递任务（幂等）；返回 accepted + 任务态。编排下沉 service 便于直测。"""
    hdr = require_headers(request)
    return await enqueue_and_serialize(
        request.app.state.session_factory,
        tenant_id=hdr["tenant_id"],
        request_id=hdr["request_id"],
        import_batch_no=body.import_batch_no,
        data_type=body.data_type,
        import_date=body.import_date,
        staging_key=body.staging_key,
        file_checksum=body.file_checksum,
        file_size=body.file_size,
        total_count=body.total_count,
    )
