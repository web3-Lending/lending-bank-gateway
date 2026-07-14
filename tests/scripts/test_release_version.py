"""[REQ-5] release_version.py 自动升版单测（纯函数 + 短路路径）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "release_version.py"


def _load():
    spec = importlib.util.spec_from_file_location("release_version_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestBumpSemver:
    def test_levels(self) -> None:
        rv = _load()
        assert rv.bump_semver("0.1.0", "patch") == "0.1.1"
        assert rv.bump_semver("0.1.7", "minor") == "0.2.0"
        assert rv.bump_semver("1.4.2", "major") == "2.0.0"

    def test_non_semver_rejected(self) -> None:
        rv = _load()
        with pytest.raises(SystemExit, match="SemVer"):
            rv.bump_semver("1.8.0-rc.1", "patch")


class TestConventionalBumpLevel:
    def test_fix_only_patch(self) -> None:
        rv = _load()
        assert rv.conventional_bump_level("fix: x\n\n\x00chore: y\n\n\x00") == "patch"

    def test_feat_minor(self) -> None:
        rv = _load()
        assert rv.conventional_bump_level("fix: x\n\n\x00feat(api): y\n\n\x00") == "minor"

    def test_breaking_bang_major(self) -> None:
        rv = _load()
        assert rv.conventional_bump_level("feat(api)!: 不兼容\n\n\x00") == "major"

    def test_breaking_footer_major(self) -> None:
        rv = _load()
        assert (
            rv.conventional_bump_level("refactor: 重排\n\nBREAKING CHANGE: 删字段\n\x00")
            == "major"
        )


class TestAutoBumpShortCircuits:
    def _stub_run(self, rv: Any, mapping: dict[tuple[str, ...], Any]) -> None:
        def fake_run(args: list[str], **kwargs: Any):
            for prefix, result in mapping.items():
                if tuple(args[: len(prefix)]) == prefix:
                    class R:
                        stdout = result
                        returncode = 0

                    return R()
            raise AssertionError(f"unexpected git call: {args}")

        rv.run = fake_run

    def test_dirty_tree_skipped(self) -> None:
        rv = _load()
        seen: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: Any):
            seen.append(list(args))

            class R:
                stdout = " M app/main.py\n"
                returncode = 0

            return R()

        rv.run = fake_run
        facts = rv.auto_bump()
        assert facts["autoBump"] == "skipped-dirty-tree"
        # 与 GIT_SHA describe --dirty 同口径：只算 tracked 改动，untracked 不阻塞升版
        assert "--untracked-files=no" in seen[0]

    def test_anchor_missing(self) -> None:
        rv = _load()
        self._stub_run(rv, {("git", "status"): "", ("git", "log"): ""})
        facts = rv.auto_bump()
        assert facts["autoBump"] == "anchor-missing"

    def test_no_change_since_anchor(self) -> None:
        rv = _load()
        anchor = "a" * 40

        def fake_run(args: list[str], **kwargs: Any):
            class R:
                returncode = 0
                stdout = ""

            if args[:3] == ["git", "log", "-1"]:
                R.stdout = anchor + "\n"
            return R()

        rv.run = fake_run
        facts = rv.auto_bump()
        assert facts["autoBump"] == "no-change"

    def test_bump_writes_and_commits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rv = _load()
        anchor = "a" * 40
        commands: list[list[str]] = []
        written: list[str] = []
        monkeypatch.setattr(rv, "write_source_version", written.append)

        def fake_run(args: list[str], **kwargs: Any):
            commands.append(list(args))

            class R:
                returncode = 0
                stdout = ""

            if args[:3] == ["git", "log", "-1"]:
                R.stdout = anchor + "\n"
            elif args[:2] == ["git", "log"]:
                R.stdout = "feat: 新能力\n\n\x00"
            return R()

        monkeypatch.setattr(rv, "run", fake_run)
        source = rv.read_source_version()
        facts = rv.auto_bump()
        assert facts["autoBump"] == "minor"
        assert facts["appVersion"] == rv.bump_semver(source, "minor")
        assert written == [rv.bump_semver(source, "minor")]
        assert any(cmd[:2] == ["git", "commit"] for cmd in commands)
