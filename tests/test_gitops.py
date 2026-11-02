from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

from aicg.gitops import GitOpsError, is_aicg_state_path, park_changes, select_work_item


def test_aicg_state_paths_are_not_pr_content():
    assert is_aicg_state_path(".aicg")
    assert is_aicg_state_path(".aicg/audit-report.json")
    assert not is_aicg_state_path("modules/mod-001/exercise-01/SOLUTION.md")


def test_select_work_item_defaults_to_first():
    items = [{"id": "fill-a"}, {"id": "fill-b"}]
    assert select_work_item(items)["id"] == "fill-a"


def test_select_work_item_finds_by_id():
    items = [{"id": "fill-a"}, {"id": "fill-b"}]
    assert select_work_item(items, work_id="fill-b")["id"] == "fill-b"


def test_select_work_item_raises_when_id_unknown():
    items = [{"id": "fill-a"}]
    with pytest.raises(GitOpsError) as excinfo:
        select_work_item(items, work_id="fill-z")
    assert "fill-z" in str(excinfo.value)
    assert "fill-a" in str(excinfo.value)


# --- park_changes: never leave a dirty checkout behind -------------------------

_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin:/usr/local/bin"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True, env=_ENV).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".aicg/\n")
    (repo / "README.md").write_text("# r\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_park_changes_moves_dirty_work_to_a_branch(tmp_path: Path, monkeypatch) -> None:
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    repo = _repo(tmp_path)
    (repo / "modules").mkdir()
    (repo / "modules" / "SOLUTION.md").write_text("# new\n")
    (repo / "README.md").write_text("# changed\n")

    branch = park_changes(repo, "depth-mod-301", today=date(2026, 10, 1))

    assert branch == "aicg/parked/2026-10-01/depth-mod-301"
    assert _git(repo, "branch", "--show-current").strip() == "main"
    assert _git(repo, "status", "--porcelain") == ""
    assert "modules/SOLUTION.md" in _git(repo, "show", "--name-only", branch)
    assert (repo / "README.md").read_text() == "# r\n"


def test_park_changes_is_a_noop_on_a_clean_tree(tmp_path: Path, monkeypatch) -> None:
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    repo = _repo(tmp_path)
    assert park_changes(repo, "x") is None
    assert _git(repo, "branch", "--list", "aicg/parked/*") == ""


def test_park_changes_ignores_runner_state_and_returns_from_a_pr_branch(
    tmp_path: Path, monkeypatch
) -> None:
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    repo = _repo(tmp_path)
    (repo / ".aicg").mkdir()
    (repo / ".aicg" / "state.json").write_text("{}")
    _git(repo, "checkout", "-q", "-B", "aicg/2026-10-01/repo/depth-x")
    (repo / "new.md").write_text("x\n")

    branch = park_changes(repo, "depth-x", today=date(2026, 10, 1))

    assert branch is not None
    assert _git(repo, "branch", "--show-current").strip() == "main"
    assert (repo / ".aicg" / "state.json").exists()  # untouched
    assert "new.md" not in _git(repo, "status", "--porcelain")
