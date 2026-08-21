"""Release-chain guards: a collapsed impact baseline, and main-repo state drift.

Both guards exist because a release step used to depend on somebody remembering
something.

1. ``require_distinct_release_base`` -- when the impact baseline resolves to the
   head commit itself (the usual way: the branch was pushed to main first, so the
   inferred ``origin/main`` baseline is HEAD), the diff is empty and the old code
   answered ``no-app-release`` / "runtime unchanged", which reads like a verdict
   about the code but only meant "I saw no changes". Following it skips the whole
   deployment. It now fails closed and names the real cause.

2. ``main_repo_release_sync`` -- ``.buildops/`` is untracked, so a release built in
   a throwaway worktree vanishes when that worktree is pruned. The release chain
   now compares the main worktree's state with the release it just produced and
   prints the copy-back commands instead of relying on the operator's memory.

The negative cases below are the point: without them either guard could be
weakened back into something that never fires.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDOPS_SCRIPT = REPO_ROOT / "scripts" / "buildops.py"


def _load_buildops():
    spec = importlib.util.spec_from_file_location(
        "buildops_release_base_guard_test", BUILDOPS_SCRIPT
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_collapsed_baseline_fails_closed_and_names_the_cause(monkeypatch) -> None:
    buildops = _load_buildops()
    sha = "a" * 40
    monkeypatch.setattr(buildops, "resolve_commit", lambda ref: sha)

    with pytest.raises(SystemExit) as excinfo:
        buildops.require_distinct_release_base("origin/main", "HEAD", [])

    message = str(excinfo.value)
    # The reader has to walk away knowing they passed the wrong baseline...
    assert "--base-ref" in message
    assert "same commit" in message
    assert sha[:12] in message
    # ...and must not be told the runtime is unchanged, which is the opposite action.
    assert "no-app-release" not in message
    assert "runtime unchanged" not in message


def test_release_impact_refuses_to_classify_a_collapsed_baseline(monkeypatch) -> None:
    buildops = _load_buildops()
    sha = "b" * 40
    monkeypatch.setattr(buildops, "resolve_ref", lambda ref: sha)
    monkeypatch.setattr(buildops, "resolve_commit", lambda ref: sha)
    monkeypatch.setattr(buildops, "changed_paths", lambda base, head: [])

    with pytest.raises(SystemExit) as excinfo:
        buildops.release_impact_facts("origin/main", "HEAD")

    assert "no-app-release" not in str(excinfo.value)


def test_annotated_tag_at_head_cannot_slip_past_the_guard(monkeypatch) -> None:
    """``git rev-parse <annotated tag>`` yields the tag object, not the commit.

    Comparing raw object shas therefore let a tag pointing at the head commit look
    like a distinct baseline while the diff was empty -- exactly the case the guard
    exists for. Reproduced against a real repository on 2026-08-21 before the refs
    were peeled with ``^{commit}``.
    """
    buildops = _load_buildops()
    tag_object = "d" * 40
    commit = "e" * 40
    monkeypatch.setattr(
        buildops, "resolve_ref", lambda ref: tag_object if ref == "v1.0" else commit
    )
    monkeypatch.setattr(buildops, "resolve_commit", lambda ref: commit)
    monkeypatch.setattr(buildops, "changed_paths", lambda base, head: [])

    with pytest.raises(SystemExit):
        buildops.release_impact_facts("v1.0", "HEAD")


def test_real_changes_against_the_same_commit_still_classify(monkeypatch) -> None:
    """A dirty worktree diffed against its own HEAD has a real change set; leave it alone."""
    buildops = _load_buildops()
    sha = "c" * 40
    changed = [buildops.APP_IMPACT_PREFIXES[0] + "main.py"]
    monkeypatch.setattr(buildops, "resolve_ref", lambda ref: sha)
    monkeypatch.setattr(buildops, "resolve_commit", lambda ref: sha)
    monkeypatch.setattr(buildops, "changed_paths", lambda base, head: changed)

    facts = buildops.release_impact_facts("origin/main", "HEAD")

    assert facts["classification"] == "app-impact"


def _fake_worktree_list(main_root: Path, current: Path, *, bare: bool = False) -> str:
    detail = "bare\n" if bare else f"HEAD {'0' * 40}\nbranch refs/heads/main\n"
    first = f"worktree {main_root}\n" + detail
    return first + f"\nworktree {current}\nHEAD {'1' * 40}\nbranch refs/heads/fix/topic\n"


def _wire_worktree(
    monkeypatch, buildops, main_root: Path, current: Path, *, bare: bool = False
) -> None:
    monkeypatch.setattr(buildops, "ROOT", current)
    monkeypatch.setattr(buildops, "RELEASES", current / ".buildops" / "releases")
    monkeypatch.setattr(buildops, "ENV_STATE", current / ".buildops" / "env-state")
    monkeypatch.setattr(
        buildops,
        "git_output",
        lambda *args: _fake_worktree_list(main_root, current, bare=bare),
    )


def test_main_repo_behind_release_reports_gaps_and_copy_back(monkeypatch, tmp_path) -> None:
    buildops = _load_buildops()
    main_root = tmp_path / "main"
    current = tmp_path / "wt" / "topic"
    main_root.mkdir()
    current.mkdir(parents=True)
    _wire_worktree(monkeypatch, buildops, main_root, current)

    sync = buildops.main_repo_release_sync("dev", "rel-1")

    assert sync["inLinkedWorktree"] is True
    assert sync["inSync"] is False
    assert len(sync["gaps"]) == 2
    joined = "\n".join(sync["copyBackCommands"])
    assert str(main_root / ".buildops" / "releases") in joined
    assert "cp -a" in joined


def test_latest_pointer_moves_only_for_a_fresh_build(monkeypatch, tmp_path) -> None:
    """Promotion and rollback copy state back without rewinding LATEST."""
    buildops = _load_buildops()
    main_root = tmp_path / "main"
    current = tmp_path / "wt" / "topic"
    main_root.mkdir()
    current.mkdir(parents=True)
    _wire_worktree(monkeypatch, buildops, main_root, current)

    promoted = buildops.main_repo_release_sync("dev", "rel-1")
    built = buildops.main_repo_release_sync("local", "rel-1", set_latest=True)

    assert not any("ln -sfn" in command for command in promoted["copyBackCommands"])
    assert any("ln -sfn" in command for command in built["copyBackCommands"])


def test_main_repo_holding_the_same_release_is_in_sync(monkeypatch, tmp_path) -> None:
    buildops = _load_buildops()
    main_root = tmp_path / "main"
    current = tmp_path / "wt" / "topic"
    current.mkdir(parents=True)
    state_dir = main_root / ".buildops" / "env-state"
    state_dir.mkdir(parents=True)
    (state_dir / "dev.json").write_text(json.dumps({"releaseId": "rel-1"}), encoding="utf-8")
    release_dir = main_root / ".buildops" / "releases" / "rel-1"
    release_dir.mkdir(parents=True)
    (release_dir / "manifest.json").write_text("{}", encoding="utf-8")
    _wire_worktree(monkeypatch, buildops, main_root, current)

    sync = buildops.main_repo_release_sync("dev", "rel-1")

    assert sync["inSync"] is True
    assert sync["gaps"] == []
    assert sync["copyBackCommands"] == []


def test_release_built_in_the_main_repo_needs_no_copy_back(monkeypatch, tmp_path) -> None:
    buildops = _load_buildops()
    main_root = tmp_path / "main"
    main_root.mkdir()
    _wire_worktree(monkeypatch, buildops, main_root, main_root)

    sync = buildops.main_repo_release_sync("local", "rel-1")

    assert sync["inLinkedWorktree"] is False
    assert sync["inSync"] is True


def test_bare_main_worktree_is_not_a_copy_back_target(monkeypatch, tmp_path) -> None:
    """A bare main "worktree" is a git directory, not a place to copy release state into."""
    buildops = _load_buildops()
    bare_root = tmp_path / "repo.git"
    current = tmp_path / "wt" / "topic"
    bare_root.mkdir()
    current.mkdir(parents=True)
    _wire_worktree(monkeypatch, buildops, bare_root, current, bare=True)

    assert buildops.main_worktree_root() is None
    assert buildops.main_repo_release_sync("dev", "rel-1")["inSync"] is True


def test_out_of_sync_report_is_printed_where_an_operator_sees_it(
    monkeypatch, tmp_path, capsys
) -> None:
    buildops = _load_buildops()
    main_root = tmp_path / "main"
    current = tmp_path / "wt" / "topic"
    main_root.mkdir()
    current.mkdir(parents=True)
    _wire_worktree(monkeypatch, buildops, main_root, current)

    sync = buildops.report_main_repo_release_sync("dev", "rel-1")

    err = capsys.readouterr().err
    assert "OUT OF SYNC" in err
    assert "cp -a" in err
    assert sync["inSync"] is False
