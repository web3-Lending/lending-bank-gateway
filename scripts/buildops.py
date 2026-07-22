#!/usr/bin/env python3
"""BuildOps adapter for lending-bank-gateway release manifests and env-state.

Release facts stay out of git under .buildops/. A formal app release starts
from a clean local build, stores an immutable docker save bundle, and promotes
that same bundle to dev/UAT with --no-build and runtime /api/version checks.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from zoneinfo import ZoneInfo


def _read_source_version() -> str:
    """appVersion 真源是 pyproject.toml（与 deploy.sh 同源，避免死常量漂移）。"""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        match = re.match(r'^version = "(.+)"', line)
        if match:
            return match.group(1)
    return "0.1.0"


PROJECT_ID = "lending-bank-gateway"
SERVICE_NAME = "lending-bank-gateway-api"
APP_VERSION = _read_source_version()
SCHEMA_REVISION = "d3e4f5a6b7c8_0021_platform_bank_account"
SCHEMA_FROM = "c9d0e1f2a3b4_0020_order_trans_type_ori_req_date"
DATA_ACTION = "none"
IMAGE_NAME = "lending-bank-gateway"
IMAGE_TAG = "latest"
LOCAL_PORT = 8022
DEV_RUNTIME_ENV = "dev-hw"
READY_PATH = "/readyz"
VERSION_PATH = "/api/version"
HKT = ZoneInfo("Asia/Hong_Kong")

ROOT = Path(__file__).resolve().parents[1]
BUILDOPS = ROOT / ".buildops"
RELEASES = BUILDOPS / "releases"
ENV_STATE = BUILDOPS / "env-state"
RELEASE_IMPACT = BUILDOPS / "release-impact"
DEPLOY = ROOT / "deploy" / "deploy.sh"
DEV_ENV = ROOT / "deploy" / "env.dev-hw"
UAT_CONTRACT = ROOT / "deploy" / "uat" / "uat-contract.json"
SECRETS = Path.home() / ".lending-deploy" / "secrets.env"

NO_APP_RELEASE_EXACT = {
    ".gitignore",
    "CLAUDE.md",
    "AGENTS.md",
    "README.md",
    "MIGRATION-MFE.md",
    "uat-deploy.config.yaml",
    "env.uat.template",
    "scripts/buildops.py",
    "scripts/ds-build.sh",
    "scripts/uat-deploy.sh",
}
NO_APP_RELEASE_PREFIXES = (
    "docs/",
    "tests/",
    ".claude/",
    ".agents/",
    ".codex/",
    ".github/",
    "scripts/release-",
    "scripts/check_doc_drift.py",
    "deploy/uat/",
)
APP_IMPACT_PREFIXES = (
    "app/",
    "alembic/",
    "alembic.ini",
    "contracts/openapi.json",
    "pyproject.toml",
    "requirements.txt",
    "deploy/Dockerfile",
    "deploy/docker-compose.yml",
    "deploy/deploy.sh",
    "deploy/entrypoint.sh",
    "deploy/env.",
)


def hkt_now() -> str:
    return datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S HKT")


def release_stamp() -> str:
    return datetime.now(HKT).strftime("%Y%m%d-%H%M%S")


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def base64_encode(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def run(
    args: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(  # noqa: S603 - args are explicit adapter-controlled command lists
        args,
        cwd=cwd,
        env=merged_env,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"required binary not found: {name}")


def git_output(*args: str) -> str:
    return run(["git", *args], capture=True).stdout.strip()


def require_clean_tree() -> None:
    status = git_output("status", "--porcelain")
    if status:
        raise SystemExit(
            "source tree is dirty; commit or stash changes before generating a release manifest"
        )


def full_git_sha() -> str:
    sha = git_output("rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise SystemExit(f"gitSha must be a full 40-char SHA, got {sha!r}")
    return sha


def resolve_ref(ref: str) -> str:
    return git_output("rev-parse", ref)


def changed_paths(base_ref: str, head_ref: str) -> list[str]:
    out = git_output(
        "-c",
        "core.quotepath=false",
        "diff",
        "--name-only",
        f"{base_ref}..{head_ref}",
    )
    return [line for line in out.splitlines() if line]


def classify_path(path: str) -> str:
    if path in NO_APP_RELEASE_EXACT or any(
        path.startswith(prefix) for prefix in NO_APP_RELEASE_PREFIXES
    ):
        return "no-app-release"
    if any(path.startswith(prefix) for prefix in APP_IMPACT_PREFIXES):
        return "app-impact"
    return "unknown"


def release_impact_facts(base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> dict[str, Any]:
    base_sha = resolve_ref(base_ref)
    head_sha = resolve_ref(head_ref)
    paths = changed_paths(base_sha, head_sha)
    categories: dict[str, list[str]] = {
        "app-impact": [],
        "no-app-release": [],
        "unknown": [],
    }
    for path in paths:
        categories[classify_path(path)].append(path)
    if categories["unknown"]:
        classification = "unknown"
    elif categories["app-impact"]:
        classification = "app-impact"
    else:
        classification = "no-app-release"
    return {
        "projectId": PROJECT_ID,
        "classification": classification,
        "baseRef": base_ref,
        "headRef": head_ref,
        "baseSha": base_sha,
        "headSha": head_sha,
        "changedPaths": paths,
        "categories": categories,
    }


def require_app_impact(base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> dict[str, Any]:
    facts = release_impact_facts(base_ref, head_ref)
    classification = facts["classification"]
    if classification == "unknown":
        unknown = ", ".join(facts["categories"]["unknown"])
        raise SystemExit(f"release-impact unknown; classify paths before release: {unknown}")
    if classification == "no-app-release":
        raise SystemExit(
            "releaseImpact=no-app-release; runtime unchanged. "
            "Use no-op evidence instead of building, transferring, or restarting app containers."
        )
    return facts


def release_impact_cmd(args: argparse.Namespace) -> None:
    facts = release_impact_facts(args.base_ref, args.head_ref)
    if args.write_noop and facts["classification"] == "no-app-release":
        states = {
            env_name: write_noop_evidence(env_name, facts, "release-impact no-app-release")
            for env_name in args.env
        }
        facts = {
            **facts,
            "noopEvidence": {
                env_name: {
                    "noOpRunId": state.get("noOpRunId"),
                    "noOpEvidencePath": state.get("noOpEvidencePath"),
                }
                for env_name, state in states.items()
            },
        }
    print_json(facts)


def release_id(git_sha: str) -> str:
    return f"{PROJECT_ID}-{release_stamp()}-{git_sha[:12]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_prefix(value: str) -> str:
    return value if value.startswith("sha256:") else f"sha256:{value}"


def sha256_unprefixed(value: str) -> str:
    return value.removeprefix("sha256:")


def image_config_digest(image: str) -> str:
    out = run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        capture=True,
    ).stdout.strip()
    if out.startswith("sha256:"):
        return out
    if re.fullmatch(r"[0-9a-fA-F]{64}", out):
        return f"sha256:{out}"
    raise SystemExit(f"unable to resolve image config digest for {image}: {out!r}")


def image_label(image: str, key: str) -> str | None:
    out = run(
        [
            "docker",
            "image",
            "inspect",
            image,
            "--format",
            f"{{{{ index .Config.Labels {json.dumps(key)} }}}}",
        ],
        capture=True,
    ).stdout.strip()
    return out or None


def docker_save_gzip(image: str, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)
    try:
        proc = subprocess.Popen(  # noqa: S603 - docker command is adapter-controlled
            ["docker", "save", image],  # noqa: S607 - PATH lookup is acceptable here
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.stdout is None:
            raise SystemExit("docker save stdout pipe was not created")
        with gzip.open(tmp_path, "wb") as gz:
            shutil.copyfileobj(proc.stdout, gz)
        stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        rc = proc.wait()
        if rc != 0:
            raise SystemExit(f"docker save failed: {stderr.strip()}")
        tmp_path.replace(dest)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return sha256_prefix(sha256_file(dest))


def release_bundle_paths(manifest_file: Path) -> tuple[Path, Path]:
    release_dir = manifest_file.parent
    return release_dir / "image.tar.gz", release_dir / "image.tar.gz.sha256"


def read_tar_sha_sidecar(sha_path: Path) -> str:
    lines = [
        line.strip() for line in sha_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if len(lines) != 1:
        raise SystemExit(f"release tar sidecar must contain exactly one checksum line: {sha_path}")
    parts = lines[0].split(maxsplit=1)
    if len(parts) != 2:
        raise SystemExit(f"release tar sidecar has invalid sha256sum format: {sha_path}")
    digest, filename = parts[0], parts[1].lstrip("*")
    if filename != "image.tar.gz":
        raise SystemExit(f"release tar sidecar must reference image.tar.gz, got {filename!r}")
    return sha256_prefix(digest)


def verify_release_bundle(manifest_file: Path, manifest: dict[str, Any]) -> tuple[Path, Path]:
    tar_path, sha_path = release_bundle_paths(manifest_file)
    if not tar_path.exists() or not sha_path.exists():
        raise SystemExit("release image.tar.gz and image.tar.gz.sha256 are required for promotion")
    expected = manifest["tarSha256"]
    sidecar_sha = read_tar_sha_sidecar(sha_path)
    if sidecar_sha != expected:
        raise SystemExit(f"release tar sidecar mismatch: {sidecar_sha} != {expected}")
    actual_sha = sha256_prefix(sha256_file(tar_path))
    if actual_sha != expected:
        raise SystemExit(f"release tar sha mismatch: {actual_sha} != {expected}")
    return tar_path, sha_path


def release_bundle_verify_command(manifest: dict[str, Any]) -> str:
    expected = shlex.quote(sha256_unprefixed(manifest["tarSha256"]))
    return (
        f"test \"$(awk 'NF {{print $1; exit}}' image.tar.gz.sha256)\" = {expected} && "
        f"test \"$(sha256sum image.tar.gz | awk '{{print $1}}')\" = {expected} && "
        "sha256sum -c image.tar.gz.sha256"
    )


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise SystemExit(f"missing env file: {path}")
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip("'\"")
    return data


def load_deploy_secrets() -> dict[str, str]:
    data: dict[str, str] = {}
    if SECRETS.exists():
        data.update(parse_env_file(SECRETS))
    if DEV_ENV.exists():
        dev_env = parse_env_file(DEV_ENV)
        data.setdefault("DEV_SSH_HOST", dev_env.get("REMOTE_SERVER", ""))
        data.setdefault("DEV_SSH_USER", dev_env.get("REMOTE_USER", ""))
        data.setdefault("DEV_SSH_PASS", dev_env.get("REMOTE_PASSWORD", ""))
        data.setdefault("DEV_REMOTE_HOME", dev_env.get("REMOTE_PATH", "/home/dev"))
    return data


def require_secret(secrets: dict[str, str], key: str, default: str | None = None) -> str:
    value = secrets.get(key) or default
    if not value:
        raise SystemExit(f"missing {key} in {SECRETS} or deploy/env.dev-hw")
    return value


def sshpass_env(password: str) -> dict[str, str]:
    return {"SSHPASS": password}


def ssh_command(user: str, host: str, remote_command: str) -> list[str]:
    return [
        "sshpass",
        "-e",
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "LogLevel=ERROR",
        f"{user}@{host}",
        remote_command,
    ]


def scp_to_remote(
    *,
    password: str,
    sources: list[Path],
    user: str,
    host: str,
    dest: str,
) -> None:
    args = [
        "sshpass",
        "-e",
        "scp",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "LogLevel=ERROR",
        *[str(source) for source in sources],
        f"{user}@{host}:{dest}",
    ]
    run(args, env=sshpass_env(password))


def remote_run(
    *,
    password: str,
    user: str,
    host: str,
    command: str,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return run(
        ssh_command(user, host, command),
        env=sshpass_env(password),
        capture=capture,
    )


def remote_http_json(
    *,
    password: str,
    user: str,
    host: str,
    url: str,
) -> dict[str, Any]:
    out = remote_run(
        password=password,
        user=user,
        host=host,
        command=f"curl -fsS --max-time 8 {shlex.quote(url)}",
        capture=True,
    ).stdout
    data = json.loads(out)
    if not isinstance(data, dict):
        raise SystemExit(f"expected JSON object from remote {url}")
    return data


def remote_wait_ready(
    *,
    password: str,
    user: str,
    host: str,
    port: int,
    path: str = READY_PATH,
    attempts: int = 40,
) -> None:
    url = f"http://localhost:{port}{path}"
    for _ in range(attempts):
        result = run(
            ssh_command(
                user,
                host,
                f"curl -fsS --max-time 5 {shlex.quote(url)} >/dev/null",
            ),
            env=sshpass_env(password),
            capture=True,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(3)
    raise SystemExit(f"remote readiness check timed out: {host}:{port}{path}")


def verify_remote_runtime_identity(
    *,
    password: str,
    user: str,
    host: str,
    port: int,
    release_env_name: str,
    release_id_value: str,
    run_id: str,
    git_sha: str,
    source_config_digest: str,
    image_digest: str | None = None,
) -> dict[str, Any]:
    version_body = remote_http_json(
        password=password,
        user=user,
        host=host,
        url=f"http://localhost:{port}{VERSION_PATH}",
    )
    data = version_body.get("data", {})
    expected_image_digest = image_digest or source_config_digest
    expected = {
        "projectId": PROJECT_ID,
        "serviceName": SERVICE_NAME,
        "appVersion": APP_VERSION,
        "releaseId": release_id_value,
        "releaseRunId": run_id,
        "releaseEnv": release_env_name,
        "gitSha": git_sha,
        "imageDigest": expected_image_digest,
        "sourceConfigDigest": source_config_digest,
        "schemaRevision": SCHEMA_REVISION,
        "dataAction": DATA_ACTION,
    }
    mismatches = {
        key: {"expected": value, "actual": data.get(key)}
        for key, value in expected.items()
        if data.get(key) != value
    }
    if mismatches:
        raise SystemExit(
            f"remote /api/version mismatch: {json.dumps(mismatches, ensure_ascii=False)}"
        )
    return version_body


def uat_via_dev_run(
    *,
    dev_password: str,
    dev_user: str,
    dev_host: str,
    uat_user: str,
    uat_host: str,
    command: str,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "sshpass",
            "-e",
            "ssh",
            "-n",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "LogLevel=ERROR",
            f"{dev_user}@{dev_host}",
            (
                "ssh -n -o StrictHostKeyChecking=no -o LogLevel=ERROR "
                f"{shlex.quote(uat_user + '@' + uat_host)} {shlex.quote(command)}"
            ),
        ],
        env=sshpass_env(dev_password),
        capture=capture,
        check=check,
    )


def uat_via_dev_http_json(
    *,
    dev_password: str,
    dev_user: str,
    dev_host: str,
    uat_user: str,
    uat_host: str,
    url: str,
) -> dict[str, Any]:
    out = uat_via_dev_run(
        dev_password=dev_password,
        dev_user=dev_user,
        dev_host=dev_host,
        uat_user=uat_user,
        uat_host=uat_host,
        command=f"curl -fsS --max-time 8 {shlex.quote(url)}",
        capture=True,
    ).stdout
    data = json.loads(out)
    if not isinstance(data, dict):
        raise SystemExit(f"expected JSON object from UAT {url}")
    return data


def uat_via_dev_wait_ready(
    *,
    dev_password: str,
    dev_user: str,
    dev_host: str,
    uat_user: str,
    uat_host: str,
    port: int,
    path: str = READY_PATH,
    attempts: int = 40,
) -> None:
    url = f"http://localhost:{port}{path}"
    for _ in range(attempts):
        result = uat_via_dev_run(
            dev_password=dev_password,
            dev_user=dev_user,
            dev_host=dev_host,
            uat_user=uat_user,
            uat_host=uat_host,
            command=f"curl -fsS --max-time 5 {shlex.quote(url)} >/dev/null",
            capture=True,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(3)
    raise SystemExit(f"UAT readiness check timed out: {uat_host}:{port}{path}")


def verify_uat_runtime_identity(
    *,
    dev_password: str,
    dev_user: str,
    dev_host: str,
    uat_user: str,
    uat_host: str,
    port: int,
    release_id_value: str,
    run_id: str,
    git_sha: str,
    source_config_digest: str,
    image_digest: str | None = None,
) -> dict[str, Any]:
    version_body = uat_via_dev_http_json(
        dev_password=dev_password,
        dev_user=dev_user,
        dev_host=dev_host,
        uat_user=uat_user,
        uat_host=uat_host,
        url=f"http://localhost:{port}{VERSION_PATH}",
    )
    data = version_body.get("data", {})
    expected_image_digest = image_digest or source_config_digest
    expected = {
        "projectId": PROJECT_ID,
        "serviceName": SERVICE_NAME,
        "appVersion": APP_VERSION,
        "releaseId": release_id_value,
        "releaseRunId": run_id,
        "releaseEnv": "uat",
        "gitSha": git_sha,
        "imageDigest": expected_image_digest,
        "sourceConfigDigest": source_config_digest,
        "schemaRevision": SCHEMA_REVISION,
        "dataAction": DATA_ACTION,
    }
    mismatches = {
        key: {"expected": value, "actual": data.get(key)}
        for key, value in expected.items()
        if data.get(key) != value
    }
    if mismatches:
        raise SystemExit(f"UAT /api/version mismatch: {json.dumps(mismatches, ensure_ascii=False)}")
    return version_body


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"expected JSON object in {path}")
    return data


def http_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=8) as resp:  # noqa: S310 - controlled local/dev URL
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"expected JSON object from {url}")
    return data


def find_latest_manifest_path() -> Path | None:
    latest = RELEASES / "LATEST"
    if latest.is_symlink() or latest.exists():
        target = latest.resolve()
        candidate = target / "manifest.json" if target.is_dir() else target
        if candidate.exists():
            return candidate
    manifests = sorted(RELEASES.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime)
    return manifests[-1] if manifests else None


def latest_manifest_path() -> Path:
    candidate = find_latest_manifest_path()
    if candidate is None:
        raise SystemExit("no release manifest found; run scripts/ds-build.sh local-verify first")
    return candidate


def manifest_path_arg(path_arg: str | None) -> Path:
    path = Path(path_arg) if path_arg else latest_manifest_path()
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise SystemExit(f"manifest not found: {path}")
    return path


def relative_to_root(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class ReleaseFacts:
    release_id: str
    git_sha: str
    image: str
    source_config_digest: str
    runtime_image_id: str
    tar_sha256: str
    build_time_hkt: str
    version_body: dict[str, Any]


def release_env(
    *,
    release_id_value: str,
    run_id: str,
    release_env_name: str,
    build_time_hkt: str,
    source_config_digest: str = "",
    image_digest: str | None = None,
    app_image: str | None = None,
) -> dict[str, str]:
    effective_image_digest = image_digest if image_digest is not None else source_config_digest
    env = {
        "GIT_SHA": full_git_sha(),
        "APP_VERSION": APP_VERSION,
        "COLLAB_RELEASE_ID": release_id_value,
        "COLLAB_RELEASE_RUN_ID": run_id,
        "COLLAB_RELEASE_ENV": release_env_name,
        "APP_SCHEMA_REVISION": SCHEMA_REVISION,
        "DATA_ACTION": DATA_ACTION,
        "BUILD_TIME_HKT": build_time_hkt,
        "SOURCE_CONFIG_DIGEST": source_config_digest,
        "IMAGE_DIGEST": effective_image_digest,
        "BUILDOPS_SKIP_RELEASE_MANIFEST": "true",
    }
    if app_image:
        env["APP_IMAGE"] = app_image
    return env


def verify_runtime_identity(
    *,
    release_id_value: str,
    run_id: str,
    git_sha: str,
    source_config_digest: str,
    image_digest: str | None = None,
) -> dict[str, Any]:
    version_body = http_json(f"http://localhost:{LOCAL_PORT}{VERSION_PATH}")
    data = version_body.get("data", {})
    expected_image_digest = image_digest or source_config_digest
    expected = {
        "projectId": PROJECT_ID,
        "serviceName": SERVICE_NAME,
        "appVersion": APP_VERSION,
        "releaseId": release_id_value,
        "releaseRunId": run_id,
        "releaseEnv": "local",
        "gitSha": git_sha,
        "imageDigest": expected_image_digest,
        "sourceConfigDigest": source_config_digest,
        "schemaRevision": SCHEMA_REVISION,
        "dataAction": DATA_ACTION,
    }
    mismatches = {
        key: {"expected": value, "actual": data.get(key)}
        for key, value in expected.items()
        if data.get(key) != value
    }
    if mismatches:
        raise SystemExit(
            f"runtime /api/version mismatch: {json.dumps(mismatches, ensure_ascii=False)}"
        )
    return version_body


def local_verify(args: argparse.Namespace) -> None:
    require_app_impact(args.base_ref, args.head_ref)
    require_clean_tree()
    git_sha = full_git_sha()
    rid = args.release_id or release_id(git_sha)
    build_time = hkt_now()
    run_id = args.run_id or f"local-verify-{rid}"
    release_dir = RELEASES / rid
    latest_image = f"{IMAGE_NAME}:{IMAGE_TAG}"
    release_image = f"{IMAGE_NAME}:{rid}"

    initial_env = release_env(
        release_id_value=rid,
        run_id=run_id,
        release_env_name="local",
        build_time_hkt=build_time,
    )
    run([str(DEPLOY), "local"], env=initial_env)
    run(["docker", "tag", latest_image, release_image])

    source_digest = image_config_digest(release_image)
    tar_path = release_dir / "image.tar.gz"
    tar_sha = docker_save_gzip(release_image, tar_path)
    (release_dir / "image.tar.gz.sha256").write_text(
        f"{tar_sha.removeprefix('sha256:')}  image.tar.gz\n",
        encoding="utf-8",
    )

    final_env = release_env(
        release_id_value=rid,
        run_id=run_id,
        release_env_name="local",
        build_time_hkt=build_time,
        source_config_digest=source_digest,
        app_image=release_image,
    )
    run([str(DEPLOY), "local", "--no-build"], env=final_env)
    version_body = verify_runtime_identity(
        release_id_value=rid,
        run_id=run_id,
        git_sha=git_sha,
        source_config_digest=source_digest,
    )

    facts = ReleaseFacts(
        release_id=rid,
        git_sha=git_sha,
        image=release_image,
        source_config_digest=source_digest,
        runtime_image_id=image_config_digest(release_image),
        tar_sha256=tar_sha,
        build_time_hkt=build_time,
        version_body=version_body,
    )
    manifest = build_manifest(facts, run_id)
    manifest_file = release_dir / "manifest.json"
    write_json(manifest_file, manifest)
    latest = RELEASES / "LATEST"
    latest.unlink(missing_ok=True)
    latest.symlink_to(release_dir.name)
    write_env_state("local", manifest, run_id, "verified", manifest_file)
    print_json(
        {
            "ok": True,
            "releaseId": rid,
            "releaseRunId": run_id,
            "manifestPath": relative_to_root(manifest_file),
            "sourceConfigDigest": source_digest,
            "tarSha256": tar_sha,
            "image": release_image,
        }
    )


def build_manifest(facts: ReleaseFacts, run_id: str) -> dict[str, Any]:
    image_created = image_label(facts.image, "org.opencontainers.image.created")
    return {
        "releaseId": facts.release_id,
        "releaseBundleId": facts.release_id,
        "projectId": PROJECT_ID,
        "appVersion": APP_VERSION,
        "versionPolicy": {
            "scheme": "semver",
            "bump": "patch",
            "source": "manual-release-tag",
        },
        "gitSha": facts.git_sha,
        "sourceTreeCleanAtBuild": True,
        "repoDigest": None,
        "sourceConfigDigest": facts.source_config_digest,
        "tarSha256": facts.tar_sha256,
        "runtimeImageId": facts.runtime_image_id,
        "imageDigest": facts.source_config_digest,
        "imageTag": facts.release_id,
        "imageCreatedAtHkt": image_created or facts.build_time_hkt,
        "services": [
            {
                "serviceName": SERVICE_NAME,
                "imageName": IMAGE_NAME,
                "imageDigest": facts.source_config_digest,
                "repoDigest": None,
                "sourceConfigDigest": facts.source_config_digest,
                "tarSha256": facts.tar_sha256,
                "runtimeImageId": facts.runtime_image_id,
                "imageTag": facts.release_id,
                "requiredSchemaRevision": SCHEMA_REVISION,
                "migrationRange": f"{SCHEMA_FROM}..{SCHEMA_REVISION}",
            }
        ],
        "schema": {
            "databaseProfile": "mysql",
            "migrationEngine": "alembic",
            "requiredSchemaRevision": SCHEMA_REVISION,
            "migrationRange": [SCHEMA_FROM, SCHEMA_REVISION],
            "destructiveChange": False,
        },
        "dataAction": DATA_ACTION,
        "createdAtHkt": facts.build_time_hkt,
        "verify": {
            "local": {
                "status": "passed",
                "runId": run_id,
                "verifiedAtHkt": hkt_now(),
                "apiVersion": facts.version_body,
            },
            "dev": {"status": "pending"},
            "uat": {"status": "pending"},
        },
        "rollbackRef": {"previousReleaseId": None},
    }


def write_env_state(
    env_name: str,
    manifest: dict[str, Any],
    run_id: str,
    status: str,
    manifest_file: Path,
) -> None:
    service = manifest["services"][0]
    state = {
        "environment": env_name,
        "projectId": PROJECT_ID,
        "serviceName": SERVICE_NAME,
        "appVersion": manifest["appVersion"],
        "releaseId": manifest["releaseId"],
        "gitSha": manifest["gitSha"],
        "imageDigest": service.get("imageDigest") or service.get("repoDigest"),
        "repoDigest": service.get("repoDigest"),
        "sourceConfigDigest": service.get("sourceConfigDigest"),
        "tarSha256": service.get("tarSha256"),
        "runtimeImageId": service.get("runtimeImageId"),
        "schemaRevision": service["requiredSchemaRevision"],
        "dataAction": manifest["dataAction"],
        "status": status,
        "verifyStatus": status,
        "verifyRunId": run_id,
        "manifestPath": relative_to_root(manifest_file),
        "verifiedAtHkt": hkt_now(),
        "updatedAtHkt": hkt_now(),
    }
    write_json(ENV_STATE / f"{env_name}.json", state)


def write_noop_evidence(env_name: str, facts: dict[str, Any], reason: str = "") -> dict[str, Any]:
    existing_state = load_env_state(env_name) if (ENV_STATE / f"{env_name}.json").exists() else {}
    run_id = f"noop-{env_name}-{release_stamp()}-{facts['headSha'][:12]}"
    evidence_path = RELEASE_IMPACT / f"{env_name}.txt"
    lines = [
        "releaseImpact=no-app-release",
        "runtime unchanged",
        "no image transfer",
        "no docker compose up",
        f"projectId={PROJECT_ID}",
        f"environment={env_name}",
        f"runId={run_id}",
        f"baseRef={facts['baseRef']}",
        f"baseSha={facts['baseSha']}",
        f"headRef={facts['headRef']}",
        f"headSha={facts['headSha']}",
        f"changedPaths={json.dumps(facts['changedPaths'], ensure_ascii=False)}",
    ]
    if reason:
        lines.append(f"reason={reason}")
    write_text(evidence_path, "\n".join(lines) + "\n")

    state = dict(existing_state)
    state.update(
        {
            "environment": env_name,
            "projectId": PROJECT_ID,
            "serviceName": SERVICE_NAME,
            "releaseImpact": "no-app-release",
            "runtimeUnchanged": True,
            "noOpRunId": run_id,
            "noOpEvidencePath": relative_to_root(evidence_path),
            "noOpBaseSha": facts["baseSha"],
            "noOpHeadSha": facts["headSha"],
            "noOpChangedPaths": facts["changedPaths"],
            "verifyStatus": "verified",
            "status": "verified",
            "updatedAtHkt": hkt_now(),
        }
    )
    write_json(ENV_STATE / f"{env_name}.json", state)
    return state


def update_manifest_verify(
    manifest_file: Path,
    env_name: str,
    status: str,
    run_id: str,
    api_version: dict[str, Any],
) -> dict[str, Any]:
    manifest = read_json(manifest_file)
    manifest.setdefault("verify", {})[env_name] = {
        "status": status,
        "runId": run_id,
        "verifiedAtHkt": hkt_now(),
        "apiVersion": api_version,
    }
    write_json(manifest_file, manifest)
    return manifest


def load_manifest(path_arg: str | None) -> tuple[Path, dict[str, Any]]:
    path = manifest_path_arg(path_arg)
    return path, read_json(path)


def verify_manifest_shape(manifest: dict[str, Any]) -> None:
    required = [
        "releaseId",
        "projectId",
        "appVersion",
        "gitSha",
        "sourceTreeCleanAtBuild",
        "imageDigest",
        "sourceConfigDigest",
        "tarSha256",
        "runtimeImageId",
        "services",
        "schema",
        "dataAction",
        "verify",
    ]
    missing = [key for key in required if key not in manifest]
    if missing:
        raise SystemExit(f"manifest missing required fields: {', '.join(missing)}")
    if manifest["projectId"] != PROJECT_ID:
        raise SystemExit(f"manifest projectId mismatch: {manifest['projectId']}")
    if manifest["appVersion"] != APP_VERSION:
        raise SystemExit(f"manifest appVersion mismatch: {manifest['appVersion']}")
    if manifest["dataAction"] != DATA_ACTION:
        raise SystemExit(f"dataAction must be {DATA_ACTION!r} for this release")
    if manifest["schema"]["requiredSchemaRevision"] != SCHEMA_REVISION:
        raise SystemExit("manifest schema revision does not match project contract")
    if not manifest["sourceConfigDigest"].startswith("sha256:"):
        raise SystemExit("manifest sourceConfigDigest must be sha256-prefixed")
    if manifest["imageDigest"] != manifest["sourceConfigDigest"]:
        raise SystemExit("manifest imageDigest must match sourceConfigDigest in save/load mode")
    if not manifest["tarSha256"].startswith("sha256:"):
        raise SystemExit("manifest tarSha256 must be sha256-prefixed")
    services = manifest.get("services")
    if not isinstance(services, list) or len(services) != 1:
        raise SystemExit("manifest must contain exactly one service for lending-bank-gateway")
    service = services[0]
    if service.get("serviceName") != SERVICE_NAME:
        raise SystemExit("manifest serviceName does not match project contract")
    if service.get("sourceConfigDigest") != manifest["sourceConfigDigest"]:
        raise SystemExit("service sourceConfigDigest must match manifest sourceConfigDigest")
    if service.get("imageDigest") != manifest["imageDigest"]:
        raise SystemExit("service imageDigest must match manifest imageDigest")
    if service.get("tarSha256") != manifest["tarSha256"]:
        raise SystemExit("service tarSha256 must match manifest tarSha256")
    local_verify_block = manifest["verify"].get("local", {})
    if local_verify_block.get("status") != "passed" or not local_verify_block.get("runId"):
        raise SystemExit("manifest requires passed local verify proof")


def load_env_state(env_name: str) -> dict[str, Any]:
    path = ENV_STATE / f"{env_name}.json"
    if not path.exists():
        raise SystemExit(f"missing {relative_to_root(path)}")
    return read_json(path)


def verify_env_state_matches(
    *,
    env_name: str,
    manifest: dict[str, Any],
    require_verified: bool,
) -> dict[str, Any]:
    state = load_env_state(env_name)
    checks = {
        "sameRelease": state.get("releaseId") == manifest["releaseId"],
        "sameImageDigest": state.get("imageDigest") == manifest["imageDigest"],
        "sameSourceConfigDigest": state.get("sourceConfigDigest") == manifest["sourceConfigDigest"],
        "sameTarSha256": state.get("tarSha256") == manifest["tarSha256"],
        "sameSchema": state.get("schemaRevision") == manifest["schema"]["requiredSchemaRevision"],
    }
    if require_verified:
        checks["verified"] = state.get("verifyStatus") == "verified" and bool(
            state.get("verifyRunId")
        )
    failed = [key for key, passed in checks.items() if not passed]
    if failed:
        raise SystemExit(f"{env_name} env-state mismatch: {', '.join(failed)}")
    return state


def plan(args: argparse.Namespace) -> None:
    impact = release_impact_facts(args.base_ref, args.head_ref)
    if impact["classification"] == "unknown":
        print_json(
            {
                "ok": False,
                "projectId": PROJECT_ID,
                "nextAction": "classify-release-impact",
                "releaseImpact": "unknown",
                "unknownPaths": impact["categories"]["unknown"],
                "baseSha": impact["baseSha"],
                "headSha": impact["headSha"],
            }
        )
        return
    if impact["classification"] == "no-app-release":
        if getattr(args, "write_noop", False):
            states = {
                env_name: write_noop_evidence(env_name, impact, "ds-build plan no-app-release")
                for env_name in ("local", "dev", "uat")
            }
            print_json(
                {
                    "ok": True,
                    "projectId": PROJECT_ID,
                    "releaseImpact": "no-app-release",
                    "nextAction": "none",
                    "runtimeUnchanged": True,
                    "noImageTransfer": True,
                    "noDockerComposeUp": True,
                    "noOpRunIds": {
                        env_name: state.get("noOpRunId") for env_name, state in states.items()
                    },
                }
            )
            return
        print_json(
            {
                "ok": True,
                "projectId": PROJECT_ID,
                "releaseImpact": "no-app-release",
                "nextAction": "noop-evidence",
                "runtimeUnchanged": True,
                "noImageTransfer": True,
                "noDockerComposeUp": True,
                "baseSha": impact["baseSha"],
                "headSha": impact["headSha"],
                "changedPaths": impact["changedPaths"],
            }
        )
        return

    manifest_file = find_latest_manifest_path()
    if manifest_file is None:
        print_json(
            {
                "ok": True,
                "projectId": PROJECT_ID,
                "nextAction": "local-verify",
                "reason": "no local verified release manifest found",
            }
        )
        return

    manifest = read_json(manifest_file)
    try:
        verify_manifest_shape(manifest)
        if manifest.get("gitSha") != full_git_sha():
            raise SystemExit(
                "latest manifest gitSha does not match current HEAD; run local-verify again"
            )
        local_state = verify_env_state_matches(
            env_name="local",
            manifest=manifest,
            require_verified=True,
        )
    except SystemExit as exc:
        print_json(
            {
                "ok": True,
                "projectId": PROJECT_ID,
                "nextAction": "local-verify",
                "releaseId": manifest.get("releaseId"),
                "reason": str(exc),
            }
        )
        return

    print_json(
        {
            "ok": True,
            "projectId": PROJECT_ID,
            "nextAction": "dev-promote-dry-run",
            "releaseId": manifest["releaseId"],
            "manifestPath": relative_to_root(manifest_file),
            "sourceConfigDigest": manifest["sourceConfigDigest"],
            "schemaRevision": manifest["schema"]["requiredSchemaRevision"],
            "localVerifyRunId": local_state.get("verifyRunId"),
        }
    )


def verify(args: argparse.Namespace) -> None:
    manifest_file, manifest = load_manifest(args.manifest)
    verify_manifest_shape(manifest)
    verify_release_bundle(manifest_file, manifest)
    results: dict[str, Any] = {
        "ok": True,
        "releaseId": manifest["releaseId"],
        "manifestPath": relative_to_root(manifest_file),
        "checks": {
            "manifestShape": True,
            "releaseBundle": True,
        },
    }
    for env_name in args.env:
        try:
            state = verify_env_state_matches(
                env_name=env_name,
                manifest=manifest,
                require_verified=True,
            )
        except SystemExit as exc:
            results["ok"] = False
            results.setdefault("env", {})[env_name] = {
                "ok": False,
                "reason": str(exc),
            }
        else:
            results.setdefault("env", {})[env_name] = {
                "ok": True,
                "verifyRunId": state.get("verifyRunId"),
            }
    print_json(results)


def dev_promote(args: argparse.Namespace) -> None:
    impact = require_app_impact(args.base_ref, args.head_ref)
    manifest_file, manifest = load_manifest(args.manifest)
    verify_manifest_shape(manifest)
    verify_release_bundle(manifest_file, manifest)
    local_state = verify_env_state_matches(
        env_name="local",
        manifest=manifest,
        require_verified=True,
    )
    if args.dry_run or not args.confirm:
        print_json(
            {
                "ok": True,
                "mode": "dry-run",
                "wouldPromote": manifest["releaseId"],
                "manifestPath": relative_to_root(manifest_file),
                "sourceConfigDigest": manifest["sourceConfigDigest"],
                "tarSha256": manifest["tarSha256"],
                "schemaRevision": manifest["schema"]["requiredSchemaRevision"],
                "localVerifyRunId": local_state.get("verifyRunId"),
                "releaseImpact": impact["classification"],
            }
        )
        return
    dev_promote_confirm(manifest_file, manifest)


def dev_promote_confirm(manifest_file: Path, manifest: dict[str, Any]) -> None:
    require_binary("sshpass")
    require_binary("docker")
    release_id_value = manifest["releaseId"]
    tar_path, sha_path = verify_release_bundle(manifest_file, manifest)
    bundle_check = release_bundle_verify_command(manifest)

    secrets = load_deploy_secrets()
    dev_host = require_secret(secrets, "DEV_SSH_HOST")
    dev_user = require_secret(secrets, "DEV_SSH_USER")
    dev_pass = require_secret(secrets, "DEV_SSH_PASS")
    dev_home = require_secret(secrets, "DEV_REMOTE_HOME", "/home/dev")
    remote_root = f"{dev_home}/{PROJECT_ID}"
    remote_release = f"{remote_root}/releases/{release_id_value}"
    image = f"{IMAGE_NAME}:{release_id_value}"
    run_id = f"dev-promote-{release_id_value}"
    release_payload = release_env(
        release_id_value=release_id_value,
        run_id=run_id,
        release_env_name=DEV_RUNTIME_ENV,
        build_time_hkt=hkt_now(),
        source_config_digest=manifest["sourceConfigDigest"],
        image_digest=manifest["imageDigest"],
        app_image=image,
    )

    remote_run(
        password=dev_pass,
        user=dev_user,
        host=dev_host,
        command=f"mkdir -p {shlex.quote(remote_release)}",
    )
    scp_to_remote(
        password=dev_pass,
        sources=[tar_path, sha_path, manifest_file],
        user=dev_user,
        host=dev_host,
        dest=f"{remote_release}/",
    )
    remote_run(
        password=dev_pass,
        user=dev_user,
        host=dev_host,
        command=(
            f"cd {shlex.quote(remote_release)} && "
            f"{bundle_check} && "
            f"gunzip -c image.tar.gz | docker load && "
            f"docker tag {shlex.quote(image)} {shlex.quote(image)}"
        ),
    )
    remote_image_id = remote_run(
        password=dev_pass,
        user=dev_user,
        host=dev_host,
        command=f"docker image inspect {shlex.quote(image)} --format '{{{{.Id}}}}'",
        capture=True,
    ).stdout.strip()
    if remote_image_id != manifest["sourceConfigDigest"]:
        raise SystemExit(
            f"dev image digest mismatch: {remote_image_id} != {manifest['sourceConfigDigest']}"
        )

    remote_run(
        password=dev_pass,
        user=dev_user,
        host=dev_host,
        command=(
            f"ln -sfn {shlex.quote(release_id_value)} "
            f"{shlex.quote(remote_root + '/releases/LATEST')}"
        ),
    )
    run([str(DEPLOY), "dev-hw", "--no-build"], env=release_payload)
    remote_wait_ready(
        password=dev_pass,
        user=dev_user,
        host=dev_host,
        port=LOCAL_PORT,
    )
    version_body = verify_remote_runtime_identity(
        password=dev_pass,
        user=dev_user,
        host=dev_host,
        port=LOCAL_PORT,
        release_env_name=DEV_RUNTIME_ENV,
        release_id_value=release_id_value,
        run_id=run_id,
        git_sha=manifest["gitSha"],
        source_config_digest=manifest["sourceConfigDigest"],
        image_digest=manifest["imageDigest"],
    )
    updated_manifest = update_manifest_verify(
        manifest_file,
        "dev",
        "passed",
        run_id,
        version_body,
    )
    scp_to_remote(
        password=dev_pass,
        sources=[manifest_file],
        user=dev_user,
        host=dev_host,
        dest=f"{remote_release}/",
    )
    write_env_state("dev", updated_manifest, run_id, "verified", manifest_file)
    print_json(
        {
            "ok": True,
            "environment": "dev",
            "releaseId": release_id_value,
            "verifyRunId": run_id,
            "manifestPath": relative_to_root(manifest_file),
            "envStatePath": relative_to_root(ENV_STATE / "dev.json"),
            "remoteReleasePath": remote_release,
            "sourceConfigDigest": manifest["sourceConfigDigest"],
        }
    )


def uat_dry_run(args: argparse.Namespace) -> None:
    impact = release_impact_facts(args.base_ref, args.head_ref)
    if impact["classification"] == "unknown":
        print_json(
            {
                "ok": False,
                "mode": "dry-run",
                "releaseImpact": "unknown",
                "blockedReason": "release-impact unknown; classify paths before UAT",
                "unknownPaths": impact["categories"]["unknown"],
                "uatContractPath": relative_to_root(UAT_CONTRACT),
                "checks": {
                    "uatContractExists": UAT_CONTRACT.exists(),
                    "devEnvStateExists": (ENV_STATE / "dev.json").exists(),
                },
            }
        )
        return
    if impact["classification"] == "no-app-release":
        if getattr(args, "write_noop", False):
            state = write_noop_evidence("uat", impact, "uat-deploy dry-run no-app-release")
            print_json(
                {
                    "ok": True,
                    "mode": "dry-run",
                    "releaseImpact": "no-app-release",
                    "runtimeUnchanged": True,
                    "noImageTransfer": True,
                    "noDockerComposeUp": True,
                    "noOpRunId": state.get("noOpRunId"),
                    "noOpEvidencePath": state.get("noOpEvidencePath"),
                }
            )
            return
        print_json(
            {
                "ok": True,
                "mode": "dry-run",
                "releaseImpact": "no-app-release",
                "runtimeUnchanged": True,
                "noImageTransfer": True,
                "noDockerComposeUp": True,
                "nextAction": "noop-evidence",
            }
        )
        return

    try:
        manifest_file, manifest = load_manifest(args.manifest)
    except SystemExit as exc:
        print_json(
            {
                "ok": False,
                "mode": "dry-run",
                "blockedReason": str(exc),
                "uatContractPath": relative_to_root(UAT_CONTRACT),
                "checks": {
                    "uatContractExists": UAT_CONTRACT.exists(),
                    "devEnvStateExists": (ENV_STATE / "dev.json").exists(),
                },
            }
        )
        return
    verify_manifest_shape(manifest)
    result: dict[str, Any] = {
        "ok": True,
        "mode": "dry-run",
        "releaseId": manifest["releaseId"],
        "manifestPath": relative_to_root(manifest_file),
        "uatContractPath": relative_to_root(UAT_CONTRACT),
        "checks": {
            "uatContractExists": UAT_CONTRACT.exists(),
            "devEnvStateExists": (ENV_STATE / "dev.json").exists(),
        },
    }
    if not UAT_CONTRACT.exists():
        result["ok"] = False
        result["blockedReason"] = "missing UAT contract"
        print_json(result)
        return

    if result["checks"]["devEnvStateExists"]:
        try:
            verify_release_bundle(manifest_file, manifest)
            dev_state = verify_env_state_matches(
                env_name="dev",
                manifest=manifest,
                require_verified=True,
            )
        except SystemExit as exc:
            result["ok"] = False
            result["blockedReason"] = str(exc)
        else:
            result["checks"].update(
                {
                    "devSameRelease": True,
                    "devSameDigest": True,
                    "devSameSchema": True,
                    "devVerified": True,
                }
            )
            result["devVerifyRunId"] = dev_state.get("verifyRunId")
    else:
        result["ok"] = False
        result["blockedReason"] = (
            "missing .buildops/env-state/dev.json; UAT requires dev verified proof"
        )
    print_json(result)


def uat_confirm(args: argparse.Namespace) -> None:
    impact = release_impact_facts(args.base_ref, args.head_ref)
    if impact["classification"] == "unknown":
        unknown = ", ".join(impact["categories"]["unknown"])
        raise SystemExit(f"release-impact unknown; classify paths before UAT: {unknown}")
    if impact["classification"] == "no-app-release":
        state = write_noop_evidence("uat", impact, "uat-deploy confirm no-app-release")
        print_json(
            {
                "ok": True,
                "environment": "uat",
                "releaseImpact": "no-app-release",
                "runtimeUnchanged": True,
                "noImageTransfer": True,
                "noDockerComposeUp": True,
                "noOpRunId": state.get("noOpRunId"),
                "noOpEvidencePath": state.get("noOpEvidencePath"),
            }
        )
        return

    manifest_file, manifest = load_manifest(args.manifest)
    verify_manifest_shape(manifest)
    verify_release_bundle(manifest_file, manifest)
    if not UAT_CONTRACT.exists():
        raise SystemExit("missing deploy/uat/uat-contract.json; UAT confirm blocked")
    dev_state = verify_env_state_matches(
        env_name="dev",
        manifest=manifest,
        require_verified=True,
    )
    uat_confirm_promote(manifest_file, manifest, dev_state)


def uat_confirm_promote(
    manifest_file: Path,
    manifest: dict[str, Any],
    dev_state: dict[str, Any],
) -> None:
    require_binary("sshpass")
    secrets = load_deploy_secrets()
    dev_host = require_secret(secrets, "DEV_SSH_HOST")
    dev_user = require_secret(secrets, "DEV_SSH_USER")
    dev_pass = require_secret(secrets, "DEV_SSH_PASS")
    dev_home = require_secret(secrets, "DEV_REMOTE_HOME", "/home/dev")
    uat_host = require_secret(secrets, "UAT_SSH_HOST")
    uat_user = require_secret(secrets, "UAT_SSH_USER")
    uat_home = require_secret(secrets, "UAT_REMOTE_HOME", "/home/uat")

    release_id_value = manifest["releaseId"]
    image = f"{IMAGE_NAME}:{release_id_value}"
    remote_root = f"{dev_home}/{PROJECT_ID}"
    dev_release = f"{remote_root}/releases/{release_id_value}"
    uat_root = f"{uat_home}/{PROJECT_ID}"
    uat_release = f"{uat_root}/releases/{release_id_value}"
    uat_env_file = f"{uat_root}/env.uat"
    run_id = f"uat-promote-{release_id_value}"
    bundle_check = release_bundle_verify_command(manifest)

    remote_run(
        password=dev_pass,
        user=dev_user,
        host=dev_host,
        command=(
            f"test -f {shlex.quote(dev_release + '/image.tar.gz')} && "
            f"test -f {shlex.quote(dev_release + '/image.tar.gz.sha256')} && "
            f"cd {shlex.quote(dev_release)} && {bundle_check}"
        ),
    )
    uat_via_dev_run(
        dev_password=dev_pass,
        dev_user=dev_user,
        dev_host=dev_host,
        uat_user=uat_user,
        uat_host=uat_host,
        command=(
            f"mkdir -p {shlex.quote(uat_release)} {shlex.quote(uat_root + '/logs')} && "
            f"chmod 777 {shlex.quote(uat_root + '/logs')} && "
            "(docker network inspect lending-network >/dev/null 2>&1 || "
            "docker network create lending-network) && "
            "(docker network inspect wedap-network >/dev/null 2>&1 || "
            "docker network create wedap-network)"
        ),
    )
    uat_via_dev_run(
        dev_password=dev_pass,
        dev_user=dev_user,
        dev_host=dev_host,
        uat_user=uat_user,
        uat_host=uat_host,
        command=(
            f"test -f {shlex.quote(uat_env_file)} && "
            f"! grep -q '<FILL-ME>' {shlex.quote(uat_env_file)}"
        ),
    )

    dev_to_uat = (
        f"SRC={shlex.quote(dev_release)}; "
        f"DST={shlex.quote(uat_release)}; "
        f"ssh -n -o StrictHostKeyChecking=no -o LogLevel=ERROR "
        f"{shlex.quote(uat_user + '@' + uat_host)} "
        f"{shlex.quote('mkdir -p ' + uat_release)} && "
        "scp -o StrictHostKeyChecking=no -o LogLevel=ERROR "
        '"$SRC/image.tar.gz" "$SRC/image.tar.gz.sha256" "$SRC/manifest.json" '
        f"{shlex.quote(uat_user + '@' + uat_host + ':' + uat_release + '/')}"
    )
    remote_run(
        password=dev_pass,
        user=dev_user,
        host=dev_host,
        command=dev_to_uat,
    )
    uat_via_dev_run(
        dev_password=dev_pass,
        dev_user=dev_user,
        dev_host=dev_host,
        uat_user=uat_user,
        uat_host=uat_host,
        command=(
            f"cd {shlex.quote(uat_release)} && "
            f"{bundle_check} && "
            f"gunzip -c image.tar.gz | docker load && "
            f"docker tag {shlex.quote(image)} {shlex.quote(image)}"
        ),
    )
    uat_image_id = uat_via_dev_run(
        dev_password=dev_pass,
        dev_user=dev_user,
        dev_host=dev_host,
        uat_user=uat_user,
        uat_host=uat_host,
        command=f"docker image inspect {shlex.quote(image)} --format '{{{{.Id}}}}'",
        capture=True,
    ).stdout.strip()
    if uat_image_id != manifest["sourceConfigDigest"]:
        raise SystemExit(
            f"UAT image digest mismatch: {uat_image_id} != {manifest['sourceConfigDigest']}"
        )

    compose = f"""# AUTO-GENERATED by scripts/uat-deploy.sh for {release_id_value}
services:
  lending-bank-gateway:
    image: {image}
    container_name: lending-bank-gateway
    ports:
      - "8022:8022"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    env_file:
      - {uat_env_file}
    environment:
      PROJECT_ID: {PROJECT_ID}
      SERVICE_NAME: {SERVICE_NAME}
      APP_VERSION: {APP_VERSION}
      GIT_SHA: {manifest["gitSha"]}
      COLLAB_RELEASE_ID: {release_id_value}
      COLLAB_RELEASE_RUN_ID: {run_id}
      COLLAB_RELEASE_ENV: uat
      IMAGE_DIGEST: {manifest["imageDigest"]}
      SOURCE_CONFIG_DIGEST: {manifest["sourceConfigDigest"]}
      APP_SCHEMA_REVISION: {SCHEMA_REVISION}
      DATA_ACTION: {DATA_ACTION}
      TZ: UTC
    volumes:
      - {uat_root}/logs:/app/logs
    networks:
      - lending-network
      - wedap-network
    restart: unless-stopped
    stop_grace_period: 60s
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://localhost:8022/readyz', timeout=5)"
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s

networks:
  lending-network:
    external: true
    name: lending-network
  wedap-network:
    external: true
    name: wedap-network
"""
    compose_b64 = base64_encode(compose)
    uat_via_dev_run(
        dev_password=dev_pass,
        dev_user=dev_user,
        dev_host=dev_host,
        uat_user=uat_user,
        uat_host=uat_host,
        command=(
            f"echo {shlex.quote(compose_b64)} | base64 -d > "
            f"{shlex.quote(uat_release + '/docker-compose.uat.yml')} && "
            f"ln -sfn releases/{shlex.quote(release_id_value)} "
            f"{shlex.quote(uat_root + '/current')} && "
            f"cd {shlex.quote(uat_release)} && "
            "docker compose -f docker-compose.uat.yml -p lending-bank-gateway-uat "
            "up -d --force-recreate --no-build"
        ),
    )
    uat_via_dev_wait_ready(
        dev_password=dev_pass,
        dev_user=dev_user,
        dev_host=dev_host,
        uat_user=uat_user,
        uat_host=uat_host,
        port=LOCAL_PORT,
    )
    version_body = verify_uat_runtime_identity(
        dev_password=dev_pass,
        dev_user=dev_user,
        dev_host=dev_host,
        uat_user=uat_user,
        uat_host=uat_host,
        port=LOCAL_PORT,
        release_id_value=release_id_value,
        run_id=run_id,
        git_sha=manifest["gitSha"],
        source_config_digest=manifest["sourceConfigDigest"],
        image_digest=manifest["imageDigest"],
    )
    updated_manifest = update_manifest_verify(
        manifest_file,
        "uat",
        "passed",
        run_id,
        version_body,
    )
    scp_to_remote(
        password=dev_pass,
        sources=[manifest_file],
        user=dev_user,
        host=dev_host,
        dest=f"{dev_release}/",
    )
    remote_run(
        password=dev_pass,
        user=dev_user,
        host=dev_host,
        command=(
            "scp -o StrictHostKeyChecking=no -o LogLevel=ERROR "
            f"{shlex.quote(dev_release + '/manifest.json')} "
            f"{shlex.quote(uat_user + '@' + uat_host + ':' + uat_release + '/')}"
        ),
    )
    write_env_state("uat", updated_manifest, run_id, "verified", manifest_file)
    print_json(
        {
            "ok": True,
            "environment": "uat",
            "releaseId": release_id_value,
            "verifyRunId": run_id,
            "devVerifyRunId": dev_state.get("verifyRunId"),
            "manifestPath": relative_to_root(manifest_file),
            "envStatePath": relative_to_root(ENV_STATE / "uat.json"),
            "sourceConfigDigest": manifest["sourceConfigDigest"],
        }
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_impact_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--base-ref", default="HEAD~1")
        p.add_argument("--head-ref", default="HEAD")

    p_plan = sub.add_parser("plan")
    add_impact_args(p_plan)
    p_plan.add_argument("--write-noop", action="store_true")
    p_plan.set_defaults(func=plan)

    p_status = sub.add_parser("status")
    add_impact_args(p_status)
    p_status.add_argument("--write-noop", action="store_true")
    p_status.set_defaults(func=plan)

    p_impact = sub.add_parser("release-impact")
    add_impact_args(p_impact)
    p_impact.add_argument("--write-noop", action="store_true")
    p_impact.add_argument("--env", action="append", choices=["local", "dev", "uat"], default=[])
    p_impact.set_defaults(func=release_impact_cmd)

    p_local = sub.add_parser("local-verify")
    add_impact_args(p_local)
    p_local.add_argument("--release-id")
    p_local.add_argument("--run-id")
    p_local.set_defaults(func=local_verify)

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--manifest")
    p_verify.add_argument(
        "--env",
        action="append",
        choices=["local", "dev", "uat"],
        default=["local"],
    )
    p_verify.set_defaults(func=verify)

    p_dev = sub.add_parser("dev-promote")
    add_impact_args(p_dev)
    p_dev.add_argument("--manifest")
    p_dev.add_argument("--dry-run", action="store_true")
    p_dev.add_argument("--confirm", action="store_true")
    p_dev.set_defaults(func=dev_promote)

    p_uat_dry = sub.add_parser("uat-dry-run")
    add_impact_args(p_uat_dry)
    p_uat_dry.add_argument("--manifest")
    p_uat_dry.add_argument("--write-noop", action="store_true")
    p_uat_dry.set_defaults(func=uat_dry_run)

    p_uat_confirm = sub.add_parser("uat-confirm")
    add_impact_args(p_uat_confirm)
    p_uat_confirm.add_argument("--manifest")
    p_uat_confirm.set_defaults(func=uat_confirm)

    args = parser.parse_args(argv)
    if getattr(args, "cmd", "") == "release-impact" and not args.env:
        args.env = ["local", "dev", "uat"]
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
