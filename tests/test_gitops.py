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


def test_git_changed_files_returns_exact_paths_with_spaces_and_unicode(
    tmp_path: Path, monkeypatch
) -> None:
    from aicg.gitops import git_changed_files

    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    repo = _repo(tmp_path)
    (repo / "my notes.md").write_text("x\n")
    (repo / "résumé.md").write_text("x\n")
    (repo / "README.md").write_text("# changed\n")
    _git(repo, "mv", "README.md", "RENAMED.md")

    changed = set(git_changed_files(repo))

    assert {"my notes.md", "résumé.md", "RENAMED.md"} <= changed
    assert all(not p.startswith('"') for p in changed)


def test_guardrail_catches_marker_in_file_with_space(tmp_path: Path, monkeypatch) -> None:
    from aicg.gitops import git_changed_files
    from aicg.guardrails import evaluate_guardrails

    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    repo = _repo(tmp_path)
    (repo / "my notes.md").write_text("<!-- needs-research: x -->\n")

    decision = evaluate_guardrails(repo, branch="aicg/x", changed_files=git_changed_files(repo))

    assert any("needs-research" in b for b in decision.blockers)


# --- branch names that git accepts ---------------------------------------------------


def _git_accepts(name: str) -> bool:
    import subprocess

    return subprocess.run(["git", "check-ref-format", "--branch", name],
                          capture_output=True, check=False).returncode == 0


def test_dotgithub_repo_gets_a_valid_branch_name():
    from datetime import date

    from aicg.gitops import branch_name, ref_segment

    assert ref_segment(".github") == "dotgithub"
    assert ref_segment("ai-infra-engineer-solutions") == "ai-infra-engineer-solutions"
    assert ref_segment("..weird name.lock") == "dotweird-name-lock"
    name = branch_name(".github", "org-profile-stale", date(2026, 7, 3))
    assert name == "aicg/2026-07-03/dotgithub/org-profile-stale"
    assert _git_accepts(name)


def test_ensure_branch_reports_whether_the_checkout_happened(tmp_path):
    import subprocess

    from aicg.gitops import ensure_branch

    repo = tmp_path / "r"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["-c", "user.email=a@b", "-c", "user.name=a",
                                                 "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    assert ensure_branch(repo, "aicg/2026-11-03/dotgithub/x") is True
    # An invalid name can't be checked out, and the caller must be told.
    assert ensure_branch(repo, "aicg/2026-11-03/.github/x") is False


def test_stage_excludes_runner_state(tmp_path):
    import subprocess

    from aicg.gitops import stage_all_except_runner_state

    repo = tmp_path / "r"
    (repo / ".aicg" / "run").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    (repo / ".aicg" / "run" / "prompt.md").write_text("prompt")
    (repo / "README.md").write_text("real change")
    assert stage_all_except_runner_state(repo) == 0
    staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True, check=True).stdout.split()
    assert staged == ["README.md"]
