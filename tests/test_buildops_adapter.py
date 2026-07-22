from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDOPS_SCRIPT = REPO_ROOT / "scripts" / "buildops.py"


def _load_buildops():
    spec = importlib.util.spec_from_file_location("buildops_adapter_test", BUILDOPS_SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict:
    # 发布身份字段跟随 buildops.py 模块常量（APP_VERSION 动态读 pyproject / SCHEMA_REVISION
    # 手工锚 alembic head），避免死常量漂移在 verify_manifest_shape 里假红。
    buildops = _load_buildops()
    release_id = "lending-bank-gateway-20260710-120000-aaaaaaaaaaaa"
    return {
        "releaseId": release_id,
        "releaseBundleId": release_id,
        "projectId": buildops.PROJECT_ID,
        "appVersion": buildops.APP_VERSION,
        "gitSha": "a" * 40,
        "sourceTreeCleanAtBuild": True,
        "repoDigest": None,
        "imageDigest": "sha256:" + "b" * 64,
        "sourceConfigDigest": "sha256:" + "b" * 64,
        "tarSha256": "sha256:" + "c" * 64,
        "runtimeImageId": "sha256:" + "b" * 64,
        "services": [
            {
                "serviceName": buildops.SERVICE_NAME,
                "imageName": "lending-bank-gateway",
                "imageDigest": "sha256:" + "b" * 64,
                "repoDigest": None,
                "sourceConfigDigest": "sha256:" + "b" * 64,
                "tarSha256": "sha256:" + "c" * 64,
                "runtimeImageId": "sha256:" + "b" * 64,
                "imageTag": release_id,
                "requiredSchemaRevision": buildops.SCHEMA_REVISION,
                "migrationRange": f"{buildops.SCHEMA_FROM}..{buildops.SCHEMA_REVISION}",
            }
        ],
        "schema": {
            "databaseProfile": "mysql",
            "migrationEngine": "alembic",
            "requiredSchemaRevision": buildops.SCHEMA_REVISION,
            "migrationRange": [buildops.SCHEMA_FROM, buildops.SCHEMA_REVISION],
            "destructiveChange": False,
        },
        "dataAction": buildops.DATA_ACTION,
        "verify": {
            "local": {
                "status": "passed",
                "runId": f"local-verify-{release_id}",
            },
            "dev": {"status": "pending"},
            "uat": {"status": "pending"},
        },
    }


def test_manifest_shape_accepts_local_verified_source_digest_manifest() -> None:
    buildops = _load_buildops()
    buildops.verify_manifest_shape(_manifest())


def test_manifest_shape_rejects_service_digest_drift() -> None:
    buildops = _load_buildops()
    manifest = _manifest()
    manifest["services"][0]["sourceConfigDigest"] = "sha256:" + "d" * 64

    with pytest.raises(SystemExit, match="service sourceConfigDigest"):
        buildops.verify_manifest_shape(manifest)


def test_release_bundle_binds_manifest_sidecar_and_tar(tmp_path: Path) -> None:
    buildops = _load_buildops()
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    manifest_path = release_dir / "manifest.json"
    tar_path = release_dir / "image.tar.gz"
    sha_path = release_dir / "image.tar.gz.sha256"
    payload = b"release artifact"
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    manifest = _manifest()
    manifest["tarSha256"] = expected
    manifest["services"][0]["tarSha256"] = expected
    tar_path.write_bytes(payload)
    sha_path.write_text(f"{expected.removeprefix('sha256:')}  image.tar.gz\n", encoding="utf-8")

    assert buildops.verify_release_bundle(manifest_path, manifest) == (tar_path, sha_path)


def test_release_bundle_rejects_sidecar_manifest_drift(tmp_path: Path) -> None:
    buildops = _load_buildops()
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    manifest_path = release_dir / "manifest.json"
    (release_dir / "image.tar.gz").write_bytes(b"release artifact")
    (release_dir / "image.tar.gz.sha256").write_text(
        f"{'d' * 64}  image.tar.gz\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="sidecar mismatch"):
        buildops.verify_release_bundle(manifest_path, _manifest())


def test_env_state_match_requires_same_release_digest_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buildops = _load_buildops()
    monkeypatch.setattr(buildops, "ENV_STATE", tmp_path)
    manifest = _manifest()
    state = {
        "releaseId": manifest["releaseId"],
        "imageDigest": manifest["imageDigest"],
        "sourceConfigDigest": manifest["sourceConfigDigest"],
        "tarSha256": manifest["tarSha256"],
        "schemaRevision": manifest["schema"]["requiredSchemaRevision"],
        "verifyStatus": "verified",
        "verifyRunId": "dev-verify-1",
    }
    (tmp_path / "dev.json").write_text(json.dumps(state), encoding="utf-8")

    assert (
        buildops.verify_env_state_matches(
            env_name="dev",
            manifest=manifest,
            require_verified=True,
        )
        == state
    )


def test_uat_dry_run_blocks_without_dev_env_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    buildops = _load_buildops()
    release_dir = tmp_path / "releases" / "rid"
    release_dir.mkdir(parents=True)
    manifest_path = release_dir / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    contract = tmp_path / "uat-contract.json"
    contract.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(buildops, "ENV_STATE", tmp_path / "env-state")
    monkeypatch.setattr(buildops, "UAT_CONTRACT", contract)
    monkeypatch.setattr(
        buildops,
        "release_impact_facts",
        lambda *_args, **_kwargs: {"classification": "app-impact"},
    )

    args = argparse.Namespace(manifest=str(manifest_path), base_ref="HEAD~1", head_ref="HEAD")
    buildops.uat_dry_run(args)

    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert body["checks"]["uatContractExists"] is True
    assert body["checks"]["devEnvStateExists"] is False
    assert "dev verified proof" in body["blockedReason"]


def test_deploy_no_build_paths_require_release_artifact_identity() -> None:
    text = (REPO_ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    assert "APP_IMAGE is required for remote --no-build promotion" in text
    assert "IMAGE_DIGEST and SOURCE_CONFIG_DIGEST are required" in text
    assert "docker compose -p ${COMPOSE_PROJECT}" in text
    assert "up -d --force-recreate --remove-orphans --no-build" in text
    assert "docker run -d" not in text


def test_release_impact_treats_openapi_snapshot_as_app_impact() -> None:
    buildops = _load_buildops()

    assert buildops.classify_path("contracts/openapi.json") == "app-impact"
