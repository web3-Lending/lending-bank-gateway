#!/usr/bin/env python3
"""[REQ-5] conventional-commits 自动升版（semantic-release 模式，gateway 适配版）。

gateway 无 buildops 发布链，「发布时刻」= deploy.sh dev-hw。锚点 = 真源
pyproject.toml 版本行最近一次变更的 commit（通常就是上一次 chore(release)
自动升版提交）。deploy.sh 在算 GIT_SHA 之前调用本脚本；NO_AUTO_BUMP=1 跳过。

档位规则（Lending项目组版本号与发布身份规范0707 §版本号规则）：
BREAKING CHANGE / `type!:` → major；`feat:` → minor；其余提交 → patch 兜底。

保守短路（均 exit 0 不 bump）：树脏（dirty 构建不产生正式版本语义）、
锚缺失、锚..HEAD 无新提交。bump 成功时写回真源并自动 chore(release) commit，
输出一行 JSON 供 deploy 日志留痕。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_SOURCE = ROOT / "pyproject.toml"

_BREAKING = re.compile(r"^[a-z]+(\([^)]*\))?!:")
_FEAT = re.compile(r"^feat(\([^)]*\))?:")


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    # S603 豁免：args 全部来自本脚本内部常量拼装（git 子命令），无用户输入。
    return subprocess.run(  # noqa: S603
        args, cwd=ROOT, text=True, check=check, capture_output=True
    )


def read_source_version() -> str:
    match = re.search(
        r'^version\s*=\s*"([^"]+)"', VERSION_SOURCE.read_text(encoding="utf-8"), re.MULTILINE
    )
    if match is None:
        raise SystemExit(f"appVersion source missing: {VERSION_SOURCE} [project].version")
    return match.group(1)


def write_source_version(new_version: str) -> None:
    text = VERSION_SOURCE.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'^version\s*=\s*"[^"]+"',
        f'version = "{new_version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise SystemExit(f"appVersion source rewrite failed: {VERSION_SOURCE}")
    VERSION_SOURCE.write_text(updated, encoding="utf-8")


def bump_semver(version: str, level: str) -> str:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version)
    if match is None:
        raise SystemExit(
            f"appVersion 非纯 SemVer（{version}），无法自动 bump；先人工改成 MAJOR.MINOR.PATCH"
        )
    major, minor, patch = (int(part) for part in match.groups())
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def conventional_bump_level(log_text: str) -> str:
    level = "patch"
    for entry in log_text.split("\x00"):
        entry = entry.strip()
        if not entry:
            continue
        subject = entry.splitlines()[0]
        if _BREAKING.match(subject) or "BREAKING CHANGE" in entry:
            return "major"
        if _FEAT.match(subject):
            level = "minor"
    return level


def auto_bump() -> dict[str, object]:
    if run(["git", "status", "--porcelain"]).stdout.strip():
        return {"autoBump": "skipped-dirty-tree", "appVersion": read_source_version()}
    anchor = run(
        ["git", "log", "-1", "--format=%H", "-G", r"^version\s*=", "--", "pyproject.toml"],
        check=False,
    ).stdout.strip()
    source_version = read_source_version()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", anchor or ""):
        return {"autoBump": "anchor-missing", "appVersion": source_version}
    log_text = run(["git", "log", "--format=%s%n%b%x00", f"{anchor}..HEAD"]).stdout
    if not log_text.strip():
        return {"autoBump": "no-change", "appVersion": source_version}
    level = conventional_bump_level(log_text)
    new_version = bump_semver(source_version, level)
    write_source_version(new_version)
    run(["git", "add", str(VERSION_SOURCE)])
    run(
        [
            "git",
            "commit",
            "-m",
            f"chore(release): appVersion {source_version} -> {new_version}（auto-bump {level}）",
        ]
    )
    return {
        "autoBump": level,
        "appVersion": new_version,
        "previousVersion": source_version,
        "bumpBaseSha": anchor,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] != "auto-bump":
        print("usage: release_version.py auto-bump", file=sys.stderr)
        return 2
    print(f"[release-version] {json.dumps(auto_bump(), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
