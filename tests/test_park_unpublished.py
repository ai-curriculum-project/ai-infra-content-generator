"""Generated work that doesn't become a PR is parked, not stranded."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aicg.org_runner import _park_unpublished_work

ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    path = tmp_path / "repo"
    path.mkdir()
    for args in (["init", "-q", "-b", "main"],):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "README.md").write_text("# r\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    (path / "SOLUTION.md").write_text("generated\n")
    return path


def _status(repo: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout


def test_failed_verification_is_parked(repo: Path) -> None:
    item = {"work_id": "depth-mod-304", "status": "verification_failed"}
    result: dict = {}
    _park_unpublished_work(repo, item, result)
    assert item["parked_branch"].endswith("/depth-mod-304")
    assert _status(repo) == ""


def test_pr_error_reason_is_recorded_and_work_parked(repo: Path) -> None:
    item = {"work_id": "depth-mod-301", "status": "verified"}
    result = {"pr": {"status": "error", "reason": "Guardrails blocked PR creation: x"}}
    _park_unpublished_work(repo, item, result)
    assert item["last_failure"].startswith("pr error: Guardrails blocked")
    assert "parked_branch" in item
    assert _status(repo) == ""


def test_opened_pr_is_left_alone(repo: Path) -> None:
    item = {"work_id": "depth-mod-302", "status": "verified"}
    result = {"pr": {"status": "opened", "pr_url": "u"}}
    _park_unpublished_work(repo, item, result)
    assert "parked_branch" not in item
    assert "SOLUTION.md" in _status(repo)
