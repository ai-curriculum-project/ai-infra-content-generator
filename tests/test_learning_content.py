"""Tests for plan-driven learning content generation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import write_file, write_minimal_manifest

from aicg.learning_content import build_module_content_prompt, generate_role_learning_content
from aicg.org_config import load_manifest


@pytest.fixture
def module() -> dict:
    return {
        "id": "mod-101-llm-fundamentals",
        "title": "LLM Fundamentals for Application Developers",
        "objectives": [
            "Call LLM APIs and reason about tokens and context windows",
            "Handle API errors, rate limits, and retries robustly",
        ],
        "exercises": [
            {"id": "exercise-01", "slug": "first-llm-api-calls"},
            {"id": "exercise-02", "slug": "tokens-context-cost"},
        ],
    }


def test_prompt_includes_objectives_and_module(module: dict) -> None:
    prompt = build_module_content_prompt(
        role_id="agentic-ai-developer",
        role_title="Agentic AI Developer",
        level=20,
        module=module,
        sibling_module_ids=["mod-101-llm-fundamentals", "mod-102-prompt-engineering"],
    )
    assert "mod-101-llm-fundamentals" in prompt
    assert "Agentic AI Developer" in prompt
    assert "Call LLM APIs and reason about tokens" in prompt


def test_prompt_lists_exercise_files_and_targets_lessons_dir(module: dict) -> None:
    prompt = build_module_content_prompt(
        role_id="agentic-ai-developer",
        role_title="Agentic AI Developer",
        level=20,
        module=module,
        sibling_module_ids=["mod-101-llm-fundamentals"],
    )
    # Targets the learning repo's lessons/<module>/ tree, not solutions.
    assert "lessons/mod-101-llm-fundamentals/" in prompt
    assert "first-llm-api-calls" in prompt
    assert "tokens-context-cost" in prompt
    # Must not author solutions.
    assert "solutions" in prompt.lower()  # mentioned only as an exclusion


def test_prompt_enforces_source_policy(module: dict) -> None:
    prompt = build_module_content_prompt(
        role_id="agentic-ai-developer",
        role_title="Agentic AI Developer",
        level=20,
        module=module,
        sibling_module_ids=["mod-101-llm-fundamentals"],
    )
    assert "needs-research" in prompt
    assert "Do NOT invent facts" in prompt


def test_prompt_excludes_sibling_modules(module: dict) -> None:
    prompt = build_module_content_prompt(
        role_id="agentic-ai-developer",
        role_title="Agentic AI Developer",
        level=20,
        module=module,
        sibling_module_ids=["mod-101-llm-fundamentals", "mod-102-prompt-engineering"],
    )
    assert "mod-102-prompt-engineering" in prompt  # named as a do-not-touch sibling


# --- skip guard: never re-author modules that already have chapters -----------


def _role_workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    learning = workspace / "ai-infra-security-learning"
    write_file(learning / "lessons/mod-001-done/01-intro.md", "# Hand-written\n")
    write_file(learning / "lessons/mod-002-stub/README.md", "# Stub\n")
    plan = {"modules": [{"id": "mod-001-done"}, {"id": "mod-002-stub"}]}
    write_file(learning / ".aicg/curriculum-plan.json", json.dumps(plan))
    return workspace, learning


def _recording_agent(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "calls.txt"
    script = write_file(
        tmp_path / "rec.py",
        f"import sys; open({str(log)!r}, 'a').write(sys.argv[1] + '\\n')\n",
    )
    return f"{sys.executable} {script} {{work_id}}", log


def test_generate_skips_modules_that_already_have_chapters(tmp_path: Path) -> None:
    workspace, _ = _role_workspace(tmp_path)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    command, log = _recording_agent(tmp_path)
    report = generate_role_learning_content(
        manifest, workspace, "security", command_override=command
    )
    assert report["modules_skipped"] == ["mod-001-done"]
    assert report["modules_generated"] == ["mod-002-stub"]
    assert log.read_text().split() == ["learning-mod-002-stub"]


def test_generate_overwrite_reauthors_existing_modules(tmp_path: Path) -> None:
    workspace, _ = _role_workspace(tmp_path)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    command, log = _recording_agent(tmp_path)
    report = generate_role_learning_content(
        manifest, workspace, "security", command_override=command, overwrite=True
    )
    assert report["modules_skipped"] == []
    assert sorted(log.read_text().split()) == ["learning-mod-001-done", "learning-mod-002-stub"]


def _timing_out_agent(tmp_path: Path, write_chapter: bool) -> str:
    # Stand-in for run-claude-content.sh hitting its hard timeout (exit 124),
    # optionally after writing the module's chapters.
    body = (
        "import sys, pathlib\n"
        "repo = pathlib.Path(sys.argv[1])\n"
        + ("(repo / 'lessons/mod-002-stub/01-chapter.md').write_text('# Chapter\\n')\n"
           if write_chapter else "")
        + "sys.exit(124)\n"
    )
    script = write_file(tmp_path / "timeout_agent.py", body)
    return f"{sys.executable} {script} {{repo}}"


def test_module_written_before_a_timeout_counts_as_authored(tmp_path: Path) -> None:
    workspace, _ = _role_workspace(tmp_path)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    report = generate_role_learning_content(
        manifest, workspace, "security",
        command_override=_timing_out_agent(tmp_path, write_chapter=True),
    )
    assert report["modules_generated"] == ["mod-002-stub"]
    assert report["modules_failed"] == []
    assert report["modules_timed_out"] == ["mod-002-stub"]


def test_timeout_with_nothing_written_is_a_failure(tmp_path: Path) -> None:
    workspace, _ = _role_workspace(tmp_path)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    report = generate_role_learning_content(
        manifest, workspace, "security",
        command_override=_timing_out_agent(tmp_path, write_chapter=False),
    )
    assert report["modules_generated"] == []
    assert report["modules_failed"] == ["mod-002-stub"]
