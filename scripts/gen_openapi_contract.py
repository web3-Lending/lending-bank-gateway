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


def main() -> int:
    spec = build_openapi(create_app())
    payload = (json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    digest = hashlib.sha256(payload).hexdigest()

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "openapi.json").write_bytes(payload)
    (DOCS_DIR / "openapi.sha256").write_text(f"{digest}  openapi.json\n", encoding="utf-8")

    operations = sum(len(item) for item in spec["paths"].values())
    print(f"wrote {DOCS_DIR / 'openapi.json'} ({operations} operations, {len(payload)} bytes)")
    print(f"wrote {DOCS_DIR / 'openapi.sha256'} ({digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
