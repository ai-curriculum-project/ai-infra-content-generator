"""Tests for needs-research marker resolution (``aicg org resolve-markers``)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import write_file, write_minimal_manifest

from aicg.marker_resolution import (
    MarkerError,
    build_marker_prompt,
    find_marker_units,
    judge_attempt,
    repos_with_markers,
    resolve_repo_markers,
)
from aicg.org_config import load_manifest

M = "<!-- needs-research: cite the source -->"


# --- detection -------------------------------------------------------------------


def test_units_are_module_dirs_ranked_by_marker_count(tmp_path: Path) -> None:
    write_file(tmp_path / "lessons/mod-101-a/01-intro.md", f"a {M}\nb {M}\n")
    write_file(tmp_path / "lessons/mod-102-b/01-intro.md", f"a {M}\n")
    write_file(tmp_path / "modules/mod-103-c/exercise-01/SOLUTION.md", f"{M}\n{M}\n{M}\n")
    units = find_marker_units(tmp_path)
    assert [(u.module_dir, u.count) for u in units] == [
        ("modules/mod-103-c", 3),
        ("lessons/mod-101-a", 2),
        ("lessons/mod-102-b", 1),
    ]


def test_markers_outside_modules_and_in_runner_state_are_ignored(tmp_path: Path) -> None:
    write_file(tmp_path / "JOB_REQUIREMENTS.md", f"{M}\n")
    write_file(tmp_path / ".aicg/notes.md", f"{M}\n")
    write_file(tmp_path / "lessons/README.md", f"{M}\n")
    assert find_marker_units(tmp_path) == []


def test_unit_lists_markers_with_file_line_and_text(tmp_path: Path) -> None:
    write_file(
        tmp_path / "lessons/mod-101-a/01-intro.md",
        "intro\nclaim <!-- needs-research: verify the $20/day figure -->\n",
    )
    (unit,) = find_marker_units(tmp_path)
    (marker,) = unit.markers
    assert marker.path == "lessons/mod-101-a/01-intro.md"
    assert marker.line == 2
    assert "verify the $20/day figure" in marker.text


def test_unsafe_module_dir_names_are_skipped(tmp_path: Path) -> None:
    write_file(tmp_path / "lessons/mod-1 --evil/01.md", f"{M}\n")
    assert find_marker_units(tmp_path) == []


# --- prompt ----------------------------------------------------------------------


def test_prompt_lists_batch_and_limits_scope(tmp_path: Path) -> None:
    for i in range(30):
        write_file(tmp_path / f"lessons/mod-101-a/{i:02d}-c.md", f"x {M}\n")
    (unit,) = find_marker_units(tmp_path)
    prompt, batch = build_marker_prompt("repo-x", unit, batch_size=25)
    assert len(batch) == 25
    assert "lessons/mod-101-a/00-c.md:1" in prompt
    assert "lessons/mod-101-a/29-c.md" not in prompt
    assert "Edit ONLY" in prompt
    assert "WebSearch" in prompt or "web search" in prompt.lower()


# --- judging an attempt (pure) ---------------------------------------------------------


def _judge(before: dict, after: dict, allowed: set, resolved_budget: int = 12):
    return judge_attempt(before, after, allowed, lines_per_marker=resolved_budget)


def test_judge_accepts_resolved_markers_in_allowed_files() -> None:
    before = {"a.md": f"x {M}\ny {M}\n"}
    after = {"a.md": "x (source: https://example.org)\ny\n"}
    verdict = _judge(before, after, {"a.md"})
    assert verdict.accepted and verdict.resolved == 2 and verdict.changed == ["a.md"]


def test_judge_rejects_edits_to_other_files() -> None:
    before = {"a.md": f"x {M}\n", "b.md": "other\n"}
    after = {"a.md": "x\n", "b.md": "rewritten\n"}
    verdict = _judge(before, after, {"a.md"})
    assert not verdict.accepted and "b.md" in verdict.reason


def test_judge_rejects_new_or_deleted_files() -> None:
    assert not _judge({"a.md": f"{M}\n"}, {"a.md": "x\n", "new.md": "y\n"}, {"a.md"}).accepted
    assert not _judge({"a.md": f"{M}\n"}, {}, {"a.md"}).accepted


def test_judge_rejects_when_no_marker_was_resolved() -> None:
    before = {"a.md": f"x {M}\n"}
    after = {"a.md": f"x changed {M}\n"}
    verdict = _judge(before, after, {"a.md"})
    assert not verdict.accepted and "resolved" in verdict.reason


def test_judge_rejects_added_markers_in_a_file() -> None:
    before = {"a.md": f"{M}\n{M}\nplain\n"}
    after = {"a.md": f"done\ndone\nplain {M}{M}{M}\n"}
    assert not _judge(before, after, {"a.md"}).accepted


def test_judge_rejects_oversized_rewrites() -> None:
    body = "".join(f"line {i}\n" for i in range(200))
    before = {"a.md": f"{M}\n" + body}
    after = {"a.md": "resolved\n" + body.replace("line", "LINE")}
    verdict = _judge(before, after, {"a.md"}, resolved_budget=12)
    assert not verdict.accepted and "lines" in verdict.reason


def test_judge_unchanged_is_not_accepted() -> None:
    before = {"a.md": f"{M}\n"}
    verdict = _judge(before, dict(before), {"a.md"})
    assert not verdict.accepted and verdict.resolved == 0


# --- integration with a fake agent -------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    ).stdout


def _setup(tmp_path: Path):
    workspace = tmp_path / "workspace"
    learning = workspace / "ai-infra-security-learning"
    write_file(learning / "lessons/mod-001-a/01-intro.md", f"Claim one. {M}\nClaim two. {M}\n")
    write_file(learning / "lessons/mod-001-a/02-more.md", "No markers here.\n")
    write_file(learning / "README.md", "# Security\n")
    write_file(learning / ".gitignore", ".aicg/\n")
    _git(learning, "init", "-q")
    _git(learning, "add", "-A")
    _git(learning, "commit", "-q", "-m", "init")
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    return workspace, learning, manifest


def _agent(tmp_path: Path, body: str) -> str:
    script = write_file(tmp_path / "agent.py", body)
    return f"{sys.executable} {script} {{prompt}}"


RESOLVE = """
import pathlib, re
p = pathlib.Path("lessons/mod-001-a/01-intro.md")
p.write_text(re.sub(r"<!--\\s*needs-research.*?-->", "(see https://example.org)", p.read_text()))
"""

TOUCH_OTHER = """
import pathlib, re
p = pathlib.Path("lessons/mod-001-a/01-intro.md")
p.write_text(re.sub(r"<!--\\s*needs-research.*?-->", "", p.read_text()))
pathlib.Path("lessons/mod-001-a/02-more.md").write_text("rewritten\\\\n")
"""


def test_resolve_copies_back_only_accepted_files(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path)
    report = resolve_repo_markers(
        manifest,
        workspace,
        "ai-infra-security-learning",
        command_override=_agent(tmp_path, RESOLVE),
    )
    assert report["units_resolved"] == ["lessons/mod-001-a"]
    assert report["markers_resolved"] == 2
    assert report["files_changed"] == ["lessons/mod-001-a/01-intro.md"]
    assert "needs-research" not in (learning / "lessons/mod-001-a/01-intro.md").read_text()
    assert _git(learning, "status", "--porcelain").strip() == "M lessons/mod-001-a/01-intro.md"


def test_resolve_rejects_out_of_scope_edits_and_leaves_repo_clean(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path)
    report = resolve_repo_markers(
        manifest,
        workspace,
        "ai-infra-security-learning",
        command_override=_agent(tmp_path, TOUCH_OTHER),
    )
    assert report["units_rejected"] == ["lessons/mod-001-a"]
    assert "02-more.md" in report["rejections"]["lessons/mod-001-a"]
    assert _git(learning, "status", "--porcelain") == ""


def test_resolve_refuses_dirty_repo(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path)
    (learning / "README.md").write_text("local edit\n")
    with pytest.raises(MarkerError, match="uncommitted"):
        resolve_repo_markers(
            manifest,
            workspace,
            "ai-infra-security-learning",
            command_override=_agent(tmp_path, RESOLVE),
        )


def test_resolve_unknown_repo_is_an_error(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path)
    with pytest.raises(MarkerError, match="not in manifest"):
        resolve_repo_markers(manifest, workspace, "nope", command_override="x")


def test_report_is_written_for_the_job(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path)
    resolve_repo_markers(
        manifest,
        workspace,
        "ai-infra-security-learning",
        command_override=_agent(tmp_path, RESOLVE),
    )
    report = json.loads((learning / ".aicg/marker-resolution-report.json").read_text())
    assert report["head_before"] and report["files_changed"]


def test_repos_with_markers_ranks_role_repos(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path)
    assert repos_with_markers(manifest, workspace) == [("ai-infra-security-learning", 2)]


def test_judge_rejects_huge_additions_on_few_lines() -> None:
    before = {"a.md": f"Claim. {M}\n"}
    after = {"a.md": "Claim (source). " + "padding " * 2000 + "\n"}
    verdict = _judge(before, after, {"a.md"})
    assert not verdict.accepted and "characters" in verdict.reason


def test_judge_allows_a_normal_citation() -> None:
    before = {"a.md": f"The fee is $175. {M}\n"}
    after = {
        "a.md": "The fee is $175 ([Delaware Division of Corporations]"
        "(https://corp.delaware.gov/frtaxcalc/)).\n"
    }
    assert _judge(before, after, {"a.md"}).accepted
