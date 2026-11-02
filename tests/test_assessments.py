"""Tests for quiz/lab backfill (``aicg org backfill-assessments``)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import write_file, write_minimal_manifest

from aicg.assessments import (
    AssessmentError,
    AssessmentGap,
    backfill_role_assessments,
    build_assessment_prompt,
    disallowed_changes,
    find_assessment_gaps,
    module_is_authored,
)
from aicg.org_config import load_manifest

QUIZ_PLACEHOLDER = "# Quizzes\n\nAuthored under the autonomous fill-in loop.\n"
LABS_PLACEHOLDER = "# Labs\n\nLabs will be authored on subsequent cycles.\n"


# --- module_is_authored -------------------------------------------------------


def _module(root: Path, name: str = "mod-201-agents") -> Path:
    mod = root / "lessons" / name
    write_file(mod / "README.md", f"# {name}\n")
    return mod


def test_numbered_chapters_count_as_authored(tmp_path: Path) -> None:
    mod = _module(tmp_path)
    write_file(mod / "01-intro.md", "# Chapter 1\n")
    assert module_is_authored(mod)


def test_single_lecture_notes_file_counts_as_authored(tmp_path: Path) -> None:
    mod = _module(tmp_path)
    write_file(mod / "lecture-notes.md", "# Notes\n")
    assert module_is_authored(mod)


def test_lecture_notes_directory_counts_as_authored(tmp_path: Path) -> None:
    mod = _module(tmp_path)
    write_file(mod / "lecture-notes" / "01-basics.md", "# Basics\n")
    assert module_is_authored(mod)


def test_readme_and_exercises_only_is_not_authored(tmp_path: Path) -> None:
    mod = _module(tmp_path)
    write_file(mod / "exercises" / "exercise-01-x.md", "# Ex\n")
    assert not module_is_authored(mod)


def test_empty_lecture_notes_directory_is_not_authored(tmp_path: Path) -> None:
    mod = _module(tmp_path)
    (mod / "lecture-notes").mkdir()
    assert not module_is_authored(mod)


# --- find_assessment_gaps -----------------------------------------------------


def _plan(*ids: str) -> dict:
    return {"modules": [{"id": i, "title": i} for i in ids]}


def _authored(
    root: Path, name: str, quiz: str | None = QUIZ_PLACEHOLDER, labs: str | None = LABS_PLACEHOLDER
) -> Path:
    mod = _module(root, name)
    write_file(mod / "01-intro.md", "# Chapter 1\n")
    if quiz is not None:
        write_file(mod / "quizzes" / "README.md", quiz)
    if labs is not None:
        write_file(mod / "labs" / "README.md", labs)
    return mod


def test_placeholder_quiz_and_labs_are_gaps(tmp_path: Path) -> None:
    _authored(tmp_path, "mod-201-a")
    gaps = find_assessment_gaps(tmp_path, _plan("mod-201-a"))
    assert gaps == [AssessmentGap("mod-201-a", needs_quiz=True, needs_labs=True)]


def test_existing_quiz_file_is_not_a_gap(tmp_path: Path) -> None:
    mod = _authored(tmp_path, "mod-201-a")
    write_file(mod / "quizzes" / "quiz-01-a.md", "# Quiz\n")
    gaps = find_assessment_gaps(tmp_path, _plan("mod-201-a"))
    assert gaps == [AssessmentGap("mod-201-a", needs_quiz=False, needs_labs=True)]


def test_quiz_written_inside_readme_is_not_a_gap(tmp_path: Path) -> None:
    real_quiz = "# Knowledge Check\n\n" + "".join(
        f"### {i}. Question\n\n- A. one\n- B. two\n\n" for i in range(1, 9)
    )
    _authored(tmp_path, "mod-201-a", quiz=real_quiz, labs=None)
    assert find_assessment_gaps(tmp_path, _plan("mod-201-a")) == []


def test_short_readme_without_marker_is_still_a_placeholder(tmp_path: Path) -> None:
    _authored(tmp_path, "mod-201-a", quiz="# Quizzes\n\nTBD.\n", labs=None)
    gaps = find_assessment_gaps(tmp_path, _plan("mod-201-a"))
    assert gaps == [AssessmentGap("mod-201-a", needs_quiz=True, needs_labs=False)]


def test_root_quiz_file_counts_as_a_quiz(tmp_path: Path) -> None:
    mod = _authored(tmp_path, "mod-201-a", labs=None)
    write_file(mod / "quiz.md", "# Quiz\n")
    assert find_assessment_gaps(tmp_path, _plan("mod-201-a")) == []


def test_modules_without_assessment_dirs_are_not_gaps(tmp_path: Path) -> None:
    # Older layout: no quizzes/ or labs/ directories at all. Don't invent them.
    _authored(tmp_path, "mod-201-a", quiz=None, labs=None)
    assert find_assessment_gaps(tmp_path, _plan("mod-201-a")) == []


def test_unauthored_modules_are_skipped(tmp_path: Path) -> None:
    mod = _module(tmp_path, "mod-201-a")
    write_file(mod / "quizzes" / "README.md", QUIZ_PLACEHOLDER)
    assert find_assessment_gaps(tmp_path, _plan("mod-201-a")) == []


def test_gaps_follow_plan_order_and_ignore_unplanned_dirs(tmp_path: Path) -> None:
    _authored(tmp_path, "mod-202-b")
    _authored(tmp_path, "mod-201-a")
    _authored(tmp_path, "mod-299-not-in-plan")
    gaps = find_assessment_gaps(tmp_path, _plan("mod-201-a", "mod-202-b", "mod-203-missing"))
    assert [g.module_id for g in gaps] == ["mod-201-a", "mod-202-b"]


# --- build_assessment_prompt ---------------------------------------------------


def test_prompt_targets_only_assessment_dirs_and_lists_sources() -> None:
    prompt = build_assessment_prompt(
        role_title="Agentic AI Engineer",
        level=30,
        module={
            "id": "mod-208-harness",
            "title": "Harness Engineering",
            "objectives": ["Explain the harness model"],
        },
        gap=AssessmentGap("mod-208-harness", needs_quiz=True, needs_labs=False),
        source_files=["lessons/mod-208-harness/01-what.md"],
    )
    assert "lessons/mod-208-harness/quizzes/" in prompt
    assert "lessons/mod-208-harness/01-what.md" in prompt
    assert "Explain the harness model" in prompt
    assert "labs/" not in prompt.split("## Scope")[0].split("## What to write")[1]
    assert "Edit ONLY" in prompt
    assert "needs-research" in prompt


def test_prompt_covers_labs_when_needed() -> None:
    prompt = build_assessment_prompt(
        role_title="R",
        level=10,
        module={"id": "mod-1", "title": "M", "objectives": []},
        gap=AssessmentGap("mod-1", needs_quiz=False, needs_labs=True),
        source_files=["lessons/mod-1/01-a.md"],
    )
    assert "lessons/mod-1/labs/" in prompt
    assert "lessons/mod-1/quizzes/" not in prompt.split("## Scope")[0]


# --- disallowed_changes --------------------------------------------------------


def test_disallowed_changes_allows_only_assessment_dirs() -> None:
    changed = [
        "lessons/mod-1/quizzes/quiz-01-x.md",
        "lessons/mod-1/quizzes/README.md",
        "lessons/mod-1/labs/lab-01-y.md",
        "lessons/mod-1/01-intro.md",
        "lessons/mod-2/quizzes/quiz-01.md",
        "README.md",
    ]
    assert disallowed_changes(changed, "mod-1") == [
        "lessons/mod-1/01-intro.md",
        "lessons/mod-2/quizzes/quiz-01.md",
        "README.md",
    ]


def test_disallowed_changes_ignores_runner_state() -> None:
    assert disallowed_changes([".aicg/prompts/x.md"], "mod-1") == []


def test_disallowed_changes_rejects_lookalike_prefix() -> None:
    assert disallowed_changes(["lessons/mod-1/quizzes-extra/a.md"], "mod-1") == [
        "lessons/mod-1/quizzes-extra/a.md"
    ]


# --- backfill_role_assessments (integration, fake agent) ------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    ).stdout


def _setup(tmp_path: Path, modules: tuple[str, ...] = ("mod-001-a", "mod-002-b")):
    workspace = tmp_path / "workspace"
    learning = workspace / "ai-infra-security-learning"
    for name in modules:
        _authored(learning, name)
    plan = {
        "title": "AI Security",
        "level": 35,
        "modules": [{"id": n, "title": n, "objectives": ["obj"]} for n in modules],
    }
    write_file(learning / ".aicg" / "curriculum-plan.json", json.dumps(plan))
    write_file(learning / ".gitignore", ".aicg/\n")
    write_file(learning / "README.md", "# Security learning\n")
    _git(learning, "init", "-q")
    _git(learning, "add", "-A")
    _git(learning, "commit", "-q", "-m", "init")
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    return workspace, learning, manifest


def _fake_agent(tmp_path: Path, body: str) -> str:
    """A fake content agent: a Python script run with the learning repo as cwd.

    It receives the prompt path, so it can parse the module id out of it the
    way a real agent reads the packet.
    """
    script = write_file(tmp_path / "fake_agent.py", body)
    return f"{sys.executable} {script} {{prompt}}"


WRITE_QUIZ = """
import re, sys, pathlib
prompt = pathlib.Path(sys.argv[1]).read_text()
mod = re.search(r"lessons/(mod-[^/]+)/quizzes/", prompt).group(1)
p = pathlib.Path("lessons") / mod / "quizzes" / f"quiz-01-{mod}.md"
p.write_text("# Quiz\\n")
"""

WRITE_OUTSIDE = """
import pathlib
pathlib.Path("README.md").write_text("vandalized\\n")
p = pathlib.Path("lessons/mod-001-a/01-intro.md")
p.write_text("rewritten\\n")
pathlib.Path("lessons/mod-001-a/quizzes/quiz-01.md").write_text("# Quiz\\n")
pathlib.Path("stray-new-file.md").write_text("x\\n")
"""


def test_backfill_fills_first_gap_only_by_default(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path)
    report = backfill_role_assessments(
        manifest,
        workspace,
        "security",
        command_override=_fake_agent(tmp_path, WRITE_QUIZ),
    )
    assert report["modules_filled"] == ["mod-001-a"]
    assert report["modules_rejected"] == []
    assert (learning / "lessons/mod-001-a/quizzes/quiz-01-mod-001-a.md").exists()
    assert not (learning / "lessons/mod-002-b/quizzes/quiz-01-mod-002-b.md").exists()
    assert report["gaps_remaining"] == 2  # mod-001-a labs + mod-002-b


def test_backfill_respects_max_modules(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path)
    report = backfill_role_assessments(
        manifest,
        workspace,
        "security",
        max_modules=2,
        command_override=_fake_agent(tmp_path, WRITE_QUIZ),
    )
    assert report["modules_filled"] == ["mod-001-a", "mod-002-b"]


def test_backfill_reverts_and_rejects_out_of_scope_edits(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path, modules=("mod-001-a",))
    report = backfill_role_assessments(
        manifest,
        workspace,
        "security",
        command_override=_fake_agent(tmp_path, WRITE_OUTSIDE),
    )
    assert report["modules_filled"] == []
    assert report["modules_rejected"] == ["mod-001-a"]
    assert "README.md" in report["rejections"]["mod-001-a"]
    # Everything the agent did is undone, including the in-scope quiz.
    assert _git(learning, "status", "--porcelain") == ""
    assert (learning / "README.md").read_text() == "# Security learning\n"
    assert not (learning / "stray-new-file.md").exists()


REWRITE_EARLIER_QUIZ = """
import re, sys, pathlib
prompt = pathlib.Path(sys.argv[1]).read_text()
mod = re.search(r"lessons/(mod-[^/]+)/quizzes/", prompt).group(1)
if mod == "mod-002-b":
    # Out of scope: tamper with the previous module's fresh, uncommitted quiz.
    pathlib.Path("lessons/mod-001-a/quizzes/quiz-01-mod-001-a.md").write_text("tampered\\n")
p = pathlib.Path("lessons") / mod / "quizzes" / f"quiz-01-{mod}.md"
p.write_text("# Quiz\\n")
"""


def test_rejection_restores_earlier_modules_work_not_head(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path)
    report = backfill_role_assessments(
        manifest,
        workspace,
        "security",
        max_modules=2,
        command_override=_fake_agent(tmp_path, REWRITE_EARLIER_QUIZ),
    )
    assert report["modules_filled"] == ["mod-001-a"]
    assert report["modules_rejected"] == ["mod-002-b"]
    # Module 1's uncommitted quiz survives with its original content...
    quiz1 = learning / "lessons/mod-001-a/quizzes/quiz-01-mod-001-a.md"
    assert quiz1.read_text() == "# Quiz\n"
    # ...and module 2's attempt is fully undone.
    assert not (learning / "lessons/mod-002-b/quizzes/quiz-01-mod-002-b.md").exists()


def test_backfill_no_change_is_reported(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path, modules=("mod-001-a",))
    report = backfill_role_assessments(
        manifest,
        workspace,
        "security",
        command_override=_fake_agent(tmp_path, "pass\n"),
    )
    assert report["modules_filled"] == []
    assert report["modules_unchanged"] == ["mod-001-a"]


def test_backfill_refuses_dirty_repo(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path, modules=("mod-001-a",))
    (learning / "README.md").write_text("local edit\n")
    with pytest.raises(AssessmentError, match="uncommitted"):
        backfill_role_assessments(
            manifest,
            workspace,
            "security",
            command_override=_fake_agent(tmp_path, WRITE_QUIZ),
        )


def test_backfill_with_no_gaps_is_a_noop(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path, modules=())
    report = backfill_role_assessments(
        manifest,
        workspace,
        "security",
        command_override=_fake_agent(tmp_path, WRITE_QUIZ),
    )
    assert report["modules_filled"] == []
    assert report["gaps_remaining"] == 0


def test_backfill_unknown_role_raises(tmp_path: Path) -> None:
    workspace, learning, manifest = _setup(tmp_path)
    with pytest.raises(AssessmentError, match="not in manifest"):
        backfill_role_assessments(manifest, workspace, "nope", command_override="x")


def test_roles_with_gaps_lists_roles_that_need_work(tmp_path: Path) -> None:
    from aicg.assessments import roles_with_gaps

    workspace, learning, manifest = _setup(tmp_path)
    assert roles_with_gaps(manifest, workspace) == ["security"]


def test_roles_with_gaps_is_empty_when_all_filled(tmp_path: Path) -> None:
    from aicg.assessments import roles_with_gaps

    workspace, learning, manifest = _setup(tmp_path, modules=())
    assert roles_with_gaps(manifest, workspace) == []
