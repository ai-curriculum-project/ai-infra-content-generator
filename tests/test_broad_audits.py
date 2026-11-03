from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import write_file, write_minimal_manifest

from aicg.learning_audit import LearningAuditError, audit_learning_repo
from aicg.nav_audit import (
    CurriculumNavError,
    audit_curriculum_nav,
    audit_org_profile,
)
from aicg.org_config import load_manifest
from aicg.pairing_audit import audit_pairing


# ---------------------------------------------------------------------------
# Learning audit
# ---------------------------------------------------------------------------


def test_learning_audit_flags_missing_module_container(tmp_path: Path) -> None:
    repo = tmp_path / "ai-infra-engineer-learning"
    repo.mkdir(parents=True)
    report = audit_learning_repo(repo)
    types = [g["type"] for g in report["gaps"]]
    assert "missing_module_container" in types
    assert report["summary"]["error_count"] >= 1


def test_learning_audit_flags_missing_readme(tmp_path: Path) -> None:
    repo = tmp_path / "ai-infra-engineer-learning"
    (repo / "modules" / "mod-001").mkdir(parents=True)
    # No README, no exercises
    report = audit_learning_repo(repo)
    types = [g["type"] for g in report["gaps"]]
    assert "missing_module_readme" in types
    assert "missing_exercises" in types


def test_learning_audit_clean_when_complete(tmp_path: Path) -> None:
    repo = tmp_path / "ai-infra-engineer-learning"
    module = repo / "modules" / "mod-001"
    write_file(
        module / "README.md",
        "# Mod 001\n\n" + "Content " * 50 + "\n",
    )
    write_file(module / "01-chapter.md", "# Chapter\n\n" + "Lecture content. " * 1600)
    write_file(module / "exercises" / "exercise-01-foo.md", "# Ex 01\n\n" + "x" * 300)
    report = audit_learning_repo(repo)
    assert report["summary"]["error_count"] == 0


def test_learning_audit_detects_placeholder_module(tmp_path: Path) -> None:
    repo = tmp_path / "ai-infra-engineer-learning"
    module = repo / "modules" / "mod-001"
    write_file(module / "README.md", "TODO\nTBD\n")
    # Written chapters, so this is a complete module with a placeholder README
    # (a skeleton module is reported as one thin_module gap instead).
    write_file(module / "01-chapter.md", "# Chapter\n\n" + "Lecture content. " * 1600)
    write_file(module / "exercises" / "exercise-01.md", "# Ex 01\n" + "x" * 300)
    report = audit_learning_repo(repo)
    types = [g["type"] for g in report["gaps"]]
    assert "placeholder_module_readme" in types


# ---------------------------------------------------------------------------
# Pairing audit
# ---------------------------------------------------------------------------


def test_pairing_audit_skips_module_only_in_learning_dedup(tmp_path: Path) -> None:
    """Duplicate of module_solution_gap from the structural audit."""
    workspace = tmp_path / "workspace"
    (workspace / "ai-infra-security-learning" / "modules" / "mod-001").mkdir(parents=True)
    (workspace / "ai-infra-security-solutions" / "modules").mkdir(parents=True)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    report = audit_pairing(manifest, workspace, state_dir=tmp_path / "state")

    types = {f["type"] for f in report["findings"]}
    assert "module_only_in_learning" not in types


def test_pairing_audit_flags_exercise_slug_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    learning = workspace / "ai-infra-security-learning" / "modules" / "mod-001"
    solutions = workspace / "ai-infra-security-solutions" / "modules" / "mod-001"
    (learning / "exercises").mkdir(parents=True)
    (solutions / "exercise-01-different-slug").mkdir(parents=True)
    write_file(learning / "exercises" / "exercise-01-threat-model.md", "")
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    report = audit_pairing(manifest, workspace, state_dir=tmp_path / "state")

    types = {f["type"] for f in report["findings"]}
    assert "exercise_slug_drift" in types


def test_pairing_audit_clean_when_perfect_pair(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    learning = workspace / "ai-infra-security-learning" / "modules" / "mod-001"
    solutions = workspace / "ai-infra-security-solutions" / "modules" / "mod-001"
    (learning / "exercises").mkdir(parents=True)
    write_file(learning / "exercises" / "exercise-01-threat-model.md", "")
    (solutions / "exercise-01-threat-model").mkdir(parents=True)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    report = audit_pairing(manifest, workspace, state_dir=tmp_path / "state")

    # No findings for that role
    role_report = next(r for r in report["roles"] if r["role"] == "security")
    assert role_report.get("finding_count", 0) == 0


def test_pairing_audit_skips_project_only_in_learning_dedup(tmp_path: Path) -> None:
    """The structural solution audit already emits project_solution_gap
    for projects only in learning; the pairing audit deliberately
    skips that type to avoid double work-items."""
    workspace = tmp_path / "workspace"
    (workspace / "ai-infra-security-learning" / "projects" / "project-01-zero-trust").mkdir(parents=True)
    (workspace / "ai-infra-security-solutions" / "projects").mkdir(parents=True)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    report = audit_pairing(manifest, workspace, state_dir=tmp_path / "state")

    types = {f["type"] for f in report["findings"]}
    assert "project_only_in_learning" not in types


def test_pairing_audit_skips_exercise_missing_in_solutions_dedup(tmp_path: Path) -> None:
    """Same dedup logic for missing-in-solutions exercises — the structural
    audit's module_solution_gap action list already covers them."""
    workspace = tmp_path / "workspace"
    learning = workspace / "ai-infra-security-learning" / "modules" / "mod-001"
    solutions = workspace / "ai-infra-security-solutions" / "modules" / "mod-001"
    # Both modules exist; learning has 2 exercises, solutions has only 1.
    (learning / "exercises").mkdir(parents=True)
    write_file(learning / "exercises" / "exercise-01-threat-model.md", "")
    write_file(learning / "exercises" / "exercise-02-iam-design.md", "")
    (solutions / "exercise-01-threat-model").mkdir(parents=True)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    report = audit_pairing(manifest, workspace, state_dir=tmp_path / "state")

    types = {f["type"] for f in report["findings"]}
    assert "exercise_missing_in_solutions" not in types


# ---------------------------------------------------------------------------
# Curriculum-nav audit
# ---------------------------------------------------------------------------


def test_curriculum_nav_flags_orphan_module(tmp_path: Path) -> None:
    repo = tmp_path / "ai-infra-engineer-solutions"
    (repo / "modules" / "mod-001").mkdir(parents=True)
    (repo / "modules" / "mod-002").mkdir(parents=True)
    write_file(repo / "CURRICULUM.md", "# Curriculum\n\n- `mod-001`\n")

    report = audit_curriculum_nav(repo)

    types = [g["type"] for g in report["findings"]]
    assert "nav_missing_reference" in types
    # mod-002 should be the orphan
    msgs = " ".join(g["message"] for g in report["findings"])
    assert "mod-002" in msgs


def test_curriculum_nav_flags_broken_reference(tmp_path: Path) -> None:
    repo = tmp_path / "ai-infra-engineer-solutions"
    (repo / "modules" / "mod-001").mkdir(parents=True)
    write_file(
        repo / "CURRICULUM.md",
        "# Curriculum\n\n- `mod-001`\n- `mod-999`\n",
    )

    report = audit_curriculum_nav(repo)

    types = [g["type"] for g in report["findings"]]
    assert "nav_broken_reference" in types


def test_curriculum_nav_flags_missing_doc_when_modules_exist(tmp_path: Path) -> None:
    repo = tmp_path / "ai-infra-engineer-solutions"
    (repo / "modules" / "mod-001").mkdir(parents=True)
    report = audit_curriculum_nav(repo)
    types = [g["type"] for g in report["findings"]]
    assert "missing_curriculum_md" in types


def test_curriculum_nav_clean_when_aligned(tmp_path: Path) -> None:
    repo = tmp_path / "ai-infra-engineer-solutions"
    (repo / "modules" / "mod-001").mkdir(parents=True)
    write_file(
        repo / "CURRICULUM.md",
        "# Curriculum\n\nSee `mod-001` in modules/mod-001.\n",
    )
    report = audit_curriculum_nav(repo)
    assert report["gap_count"] == 0


# ---------------------------------------------------------------------------
# Org-profile audit
# ---------------------------------------------------------------------------


def test_org_profile_flags_missing_repos(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    dotgithub = manifest.repo_path(workspace, ".github")
    (dotgithub / "profile").mkdir(parents=True)
    # Empty profile README
    write_file(
        dotgithub / "profile" / "README.md",
        "# AI Infra Curriculum\n\n" + "Welcome." * 30,
    )

    report = audit_org_profile(manifest, workspace, state_dir=tmp_path / "state")

    types = [f["type"] for f in report["findings"]]
    assert "profile_missing_repos" in types


def test_org_profile_flags_thin_doc(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    dotgithub = manifest.repo_path(workspace, ".github")
    (dotgithub / "profile").mkdir(parents=True)
    write_file(dotgithub / "profile" / "README.md", "# Hi")

    report = audit_org_profile(manifest, workspace, state_dir=tmp_path / "state")

    types = [f["type"] for f in report["findings"]]
    assert "profile_too_thin" in types


def test_org_profile_flags_missing_repo_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    report = audit_org_profile(manifest, workspace, state_dir=tmp_path / "state")
    types = [f["type"] for f in report["findings"]]
    assert "missing_profile_repo" in types


# ---------------------------------------------------------------------------
# Integration: run_org_audit splices the new types into the queue
# ---------------------------------------------------------------------------


def test_run_org_audit_includes_pairing_findings(tmp_path: Path) -> None:
    """Trigger an exercise_slug_drift — that's a non-deduped pairing finding."""
    from aicg.org_runner import run_org_audit

    workspace = tmp_path / "workspace"
    learning_mod = (
        workspace / "ai-infra-security-learning" / "modules" / "mod-001"
    )
    solution_mod = (
        workspace / "ai-infra-security-solutions" / "modules" / "mod-001"
    )
    (learning_mod / "exercises").mkdir(parents=True)
    write_file(learning_mod / "README.md", "# Mod 001\n\n" + "Content " * 50)
    write_file(
        learning_mod / "exercises" / "exercise-01-threat-model.md",
        "# Ex\n" + "x" * 400,
    )
    # Same exercise number, DIFFERENT slug on the solutions side → slug drift.
    (solution_mod / "exercise-01-completely-different-slug").mkdir(parents=True)
    write_file(workspace / "ai-infra-security-solutions" / "modules" / "README.md", "")
    write_file(
        workspace / "ai-infra-security-solutions" / ".github" / "workflows" / "ci.yml",
        "name: CI\n",
    )

    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    queue = run_org_audit(manifest, workspace, state_dir=tmp_path / "state")

    types = {item.get("type") for item in queue["work_items"]}
    assert "pairing_mismatch" in types


# ---------------------------------------------------------------------------
# Cross-repo handler dispatch
# ---------------------------------------------------------------------------


def test_cross_repo_handler_resolves_org_profile_target(tmp_path: Path) -> None:
    from aicg.org_runner import _resolve_cross_repo_target

    workspace = tmp_path / "workspace"
    (workspace / ".github" / "profile").mkdir(parents=True)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    item = {
        "type": "org_profile_stale",
        "subtype": "profile_missing_repos",
        "path": "profile/README.md",
    }
    target = _resolve_cross_repo_target(manifest, workspace, item)
    assert target is not None
    assert target["repo"] == ".github"
    assert target["target_file"] == "profile/README.md"


def test_cross_repo_handler_resolves_learning_gap_target(tmp_path: Path) -> None:
    from aicg.org_runner import _resolve_cross_repo_target

    workspace = tmp_path / "workspace"
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    item = {
        "type": "learning_gap",
        "subtype": "missing_module_readme",
        "repo": "ai-infra-security-learning",
        "path": "modules/mod-001/README.md",
    }
    target = _resolve_cross_repo_target(manifest, workspace, item)
    assert target is not None
    assert target["repo"] == "ai-infra-security-learning"
    assert target["target_file"] == "modules/mod-001/README.md"


def test_cross_repo_handler_defers_project_only_in_solutions(tmp_path: Path) -> None:
    """That subtype needs human judgment — handler returns None."""
    from aicg.org_runner import _resolve_cross_repo_target

    workspace = tmp_path / "workspace"
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    item = {
        "type": "pairing_mismatch",
        "subtype": "project_only_in_solutions",
        "role": "security",
        "solution_path": "projects/project-99-orphan",
    }
    target = _resolve_cross_repo_target(manifest, workspace, item)
    assert target is None


def test_cross_repo_handler_routes_slug_drift_to_solutions(tmp_path: Path) -> None:
    from aicg.org_runner import _resolve_cross_repo_target

    workspace = tmp_path / "workspace"
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    item = {
        "type": "pairing_mismatch",
        "subtype": "exercise_slug_drift",
        "role": "security",
        "learning_path": "mod-001/exercise-01-good-slug",
        "solution_path": "mod-001/exercise-01-bad-slug",
    }
    target = _resolve_cross_repo_target(manifest, workspace, item)
    assert target is not None
    assert target["repo"] == "ai-infra-security-solutions"


def test_cross_repo_prompt_includes_details(tmp_path: Path) -> None:
    from aicg.org_runner import _build_cross_repo_prompt

    item = {
        "type": "learning_gap",
        "subtype": "missing_module_readme",
        "details": "Module mod-001 missing README",
        "path": "modules/mod-001/README.md",
    }
    target = {"repo": "ai-infra-security-learning", "target_file": "modules/mod-001/README.md"}
    prompt = _build_cross_repo_prompt(item, target)
    assert "missing_module_readme" in prompt
    assert "Module mod-001 missing README" in prompt
    assert "ai-infra-security-learning" in prompt
