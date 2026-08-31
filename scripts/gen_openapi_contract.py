#!/usr/bin/env python3
"""Regenerate the checked-in API contract artefacts under ``docs/api-contract/``.

Writes:
  * ``openapi.json``   -- the document ``build_openapi`` serves at ``/openapi.json``,
                          serialized canonically (sorted keys, 2-space indent).
  * ``openapi.sha256`` -- sha256 of exactly those bytes, which the G10 gate in
                          ``tests/test_openapi_contract_gate.py`` re-derives.

Run after any change to ``app/core/openapi_contract.OPERATION_CONTRACTS`` or to a
route signature, then review the diff -- that review is the point of checking the
document in.

    .venv/bin/python scripts/gen_openapi_contract.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.core.openapi_contract import build_openapi  # noqa: E402
from app.main import create_app  # noqa: E402

DOCS_DIR = REPO_ROOT / "docs" / "api-contract"

#: 跟在 digest 行后面的口径自述。**必须由本脚本写出**：它原本是手工补进
#: ``openapi.sha256`` 的，而本脚本每次重写整份文件——2026-08-31 重跑生成器时就这样把它
#: 静默抹掉了一次。消费方少了这两句会去猜 digest 到底是文件字节还是规范化序列化，
#: 猜错就会误判产物被篡改。``tests/test_openapi_contract_gate.py`` 有对应断言守着。
SHA256_SELF_DESCRIPTION = """#
# 口径：本文件第一个 token 是 openapi.json 的**文件字节** sha256。
# 因此 `sha256sum -c openapi.sha256` 在本仓可以直接用来校验产物完整性。
# （注意：同批四仓口径不统一——baffle 与 lending-recon 用的是「规范化序列化」digest，
#  它们的 .sha256 无法用 sha256sum -c 校验，各自文件内有说明。）
"""


def main() -> int:
    spec = build_openapi(create_app())
    payload = (json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    digest = hashlib.sha256(payload).hexdigest()

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "openapi.json").write_bytes(payload)
    (DOCS_DIR / "openapi.sha256").write_text(
        f"{digest}  openapi.json\n" + SHA256_SELF_DESCRIPTION, encoding="utf-8"
    )

    operations = sum(len(item) for item in spec["paths"].values())
    print(f"wrote {DOCS_DIR / 'openapi.json'} ({operations} operations, {len(payload)} bytes)")
    print(f"wrote {DOCS_DIR / 'openapi.sha256'} ({digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
