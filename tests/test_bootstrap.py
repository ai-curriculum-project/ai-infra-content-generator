from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import write_minimal_manifest

from aicg.bootstrap import BootstrapError, bootstrap_role
from aicg.org_config import load_manifest


def test_bootstrap_creates_paired_repos(tmp_path: Path) -> None:
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"

    report = bootstrap_role(
        manifest=manifest,
        workspace=workspace,
        role_id="data-engineer",
        title="Data Engineer",
        level=25,
        state_dir=state_dir,
    )

    learning = workspace / "ai-infra-data-engineer-learning"
    solutions = workspace / "ai-infra-data-engineer-solutions"

    assert learning.is_dir()
    assert solutions.is_dir()
    for required in (
        "README.md",
        "LICENSE",
        ".gitignore",
        "CURRICULUM.md",
        "PREREQUISITES.md",
        "VERSIONS.md",
        "lessons/README.md",
        "projects/README.md",
        ".github/workflows/ci.yml",
        ".markdownlint.jsonc",
    ):
        assert (learning / required).exists(), f"missing {required} in learning"
    for required in (
        "README.md",
        "LICENSE",
        ".gitignore",
        "SOLUTIONS_INDEX.md",
        "modules/README.md",
        "projects/README.md",
        ".github/workflows/ci.yml",
        ".markdownlint.jsonc",
    ):
        assert (solutions / required).exists(), f"missing {required} in solutions"

    prompt = report["plan"]["prompt_path"]
    text = Path(prompt).read_text(encoding="utf-8")
    assert "Phase 1 — Research" in text
    assert "data-engineer" in text
    assert "curriculum-plan.json" in text


def test_bootstrap_refuses_existing_directory(tmp_path: Path) -> None:
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    workspace = tmp_path / "workspace"
    (workspace / "ai-infra-data-engineer-learning").mkdir(parents=True)
    (workspace / "ai-infra-data-engineer-learning" / "existing.md").write_text(
        "do not clobber\n", encoding="utf-8"
    )

    with pytest.raises(BootstrapError) as excinfo:
        bootstrap_role(
            manifest=manifest,
            workspace=workspace,
            role_id="data-engineer",
            title="Data Engineer",
            level=25,
        )
    assert "already exists" in str(excinfo.value)


def test_bootstrap_overwrite_keeps_existing_unrelated_files(tmp_path: Path) -> None:
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    workspace = tmp_path / "workspace"
    learning = workspace / "ai-infra-data-engineer-learning"
    learning.mkdir(parents=True)
    legacy = learning / "legacy.md"
    legacy.write_text("legacy note\n", encoding="utf-8")

    bootstrap_role(
        manifest=manifest,
        workspace=workspace,
        role_id="data-engineer",
        title="Data Engineer",
        level=25,
        overwrite=True,
    )

    assert legacy.read_text(encoding="utf-8") == "legacy note\n"
    assert (learning / "README.md").exists()


def test_bootstrap_rejects_invalid_role_id(tmp_path: Path) -> None:
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    workspace = tmp_path / "workspace"
    with pytest.raises(BootstrapError):
        bootstrap_role(
            manifest=manifest,
            workspace=workspace,
            role_id="Bad ID!",
            title="Bad",
            level=10,
        )


def test_bootstrap_appends_role_to_json_manifest(tmp_path: Path) -> None:
    manifest_path = write_minimal_manifest(tmp_path / "aicg-org.yaml")
    manifest = load_manifest(manifest_path)
    workspace = tmp_path / "workspace"

    bootstrap_role(
        manifest=manifest,
        workspace=workspace,
        role_id="data-engineer",
        title="Data Engineer",
        level=25,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    role_ids = [role["id"] for role in payload["roles"]]
    assert "data-engineer" in role_ids
    new_role = next(role for role in payload["roles"] if role["id"] == "data-engineer")
    assert new_role["learning_repo"] == "ai-infra-data-engineer-learning"
    assert new_role["solution_repo"] == "ai-infra-data-engineer-solutions"
    assert new_role["level"] == 25


def test_bootstrap_no_update_manifest_leaves_file_unchanged(tmp_path: Path) -> None:
    manifest_path = write_minimal_manifest(tmp_path / "aicg-org.yaml")
    original = manifest_path.read_text(encoding="utf-8")
    manifest = load_manifest(manifest_path)
    workspace = tmp_path / "workspace"

    bootstrap_role(
        manifest=manifest,
        workspace=workspace,
        role_id="data-engineer",
        title="Data Engineer",
        level=25,
        write_manifest=False,
    )

    assert manifest_path.read_text(encoding="utf-8") == original


def _curriculum_plan() -> dict:
    return {
        "schema_version": 1,
        "role_id": "data-engineer",
        "title": "Data Engineer",
        "level": 25,
        "modules": [
            {
                "id": "mod-101-foundations",
                "title": "Foundations",
                "hours": 10,
                "objectives": ["Build a pipeline", "Operate a stream"],
                "exercises": [
                    {"id": "exercise-01", "slug": "intro", "hours": 1},
                    {"id": "exercise-02", "slug": "containers", "hours": 2},
                ],
                "labs": [{"id": "lab-01", "title": "First pipeline"}],
                "quizzes": 1,
            },
            {
                "id": "mod-102-streaming",
                "title": "Streaming",
                "exercises": [
                    {"id": "exercise-01", "slug": "kafka", "hours": 2},
                ],
            },
        ],
        "projects": [
            {"id": "project-101-batch-pipeline", "title": "Batch pipeline", "hours": 20}
        ],
    }


def _manifest_with_role(tmp_path: Path) -> tuple[Path, Path]:
    """Build a manifest that already has the data-engineer role."""
    manifest_path = write_minimal_manifest(tmp_path / "aicg-org.yaml")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["roles"].append(
        {
            "id": "data-engineer",
            "title": "Data Engineer",
            "level": 25,
            "learning_repo": "ai-infra-data-engineer-learning",
            "solution_repo": "ai-infra-data-engineer-solutions",
        }
    )
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return manifest_path, tmp_path / "workspace"


def test_execute_plan_scaffolds_modules_and_projects(tmp_path: Path) -> None:
    from aicg.bootstrap import execute_curriculum_plan

    manifest_path, workspace = _manifest_with_role(tmp_path)
    manifest = load_manifest(manifest_path)
    bootstrap_role(
        manifest=manifest,
        workspace=workspace,
        role_id="data-engineer",
        title="Data Engineer",
        level=25,
        write_manifest=False,
    )

    learning = workspace / "ai-infra-data-engineer-learning"
    plan_path = learning / ".aicg" / "curriculum-plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(_curriculum_plan()), encoding="utf-8")

    report = execute_curriculum_plan(
        manifest=manifest,
        workspace=workspace,
        role_id="data-engineer",
        state_dir=tmp_path / "state",
    )

    assert report["modules_created"] == ["mod-101-foundations", "mod-102-streaming"]
    assert report["projects_created"] == ["project-101-batch-pipeline"]

    mod1 = learning / "lessons" / "mod-101-foundations"
    assert (mod1 / "README.md").exists()
    assert (mod1 / "resources.md").exists()
    assert (mod1 / "labs" / "README.md").exists()
    assert (mod1 / "exercises" / "exercise-01-intro.md").exists()
    assert (mod1 / "exercises" / "exercise-02-containers.md").exists()

    solutions = workspace / "ai-infra-data-engineer-solutions"
    assert (solutions / "modules" / "mod-101-foundations" / "README.md").exists()
    assert (
        solutions
        / "modules"
        / "mod-101-foundations"
        / "exercise-01-intro"
        / "README.md"
    ).exists()

    assert (
        learning / "projects" / "project-101-batch-pipeline" / "README.md"
    ).exists()
    assert (
        solutions / "projects" / "project-101-batch-pipeline" / "README.md"
    ).exists()


def test_execute_plan_idempotent_without_overwrite(tmp_path: Path) -> None:
    from aicg.bootstrap import execute_curriculum_plan

    manifest_path, workspace = _manifest_with_role(tmp_path)
    manifest = load_manifest(manifest_path)
    bootstrap_role(
        manifest=manifest,
        workspace=workspace,
        role_id="data-engineer",
        title="Data Engineer",
        level=25,
        write_manifest=False,
    )
    plan_path = workspace / "ai-infra-data-engineer-learning" / ".aicg" / "curriculum-plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(_curriculum_plan()), encoding="utf-8")

    first = execute_curriculum_plan(
        manifest=manifest,
        workspace=workspace,
        role_id="data-engineer",
        state_dir=tmp_path / "state",
    )
    second = execute_curriculum_plan(
        manifest=manifest,
        workspace=workspace,
        role_id="data-engineer",
        state_dir=tmp_path / "state",
    )
    assert first["modules_created"] and not first["modules_skipped"]
    assert not second["modules_created"]
    assert second["modules_skipped"] == ["mod-101-foundations", "mod-102-streaming"]


def test_execute_plan_rejects_missing_plan(tmp_path: Path) -> None:
    from aicg.bootstrap import CurriculumPlanError, execute_curriculum_plan

    manifest_path, workspace = _manifest_with_role(tmp_path)
    manifest = load_manifest(manifest_path)
    bootstrap_role(
        manifest=manifest,
        workspace=workspace,
        role_id="data-engineer",
        title="Data Engineer",
        level=25,
        write_manifest=False,
    )

    with pytest.raises(CurriculumPlanError):
        execute_curriculum_plan(
            manifest=manifest,
            workspace=workspace,
            role_id="data-engineer",
            state_dir=tmp_path / "state",
        )


def test_execute_plan_rejects_unknown_role(tmp_path: Path) -> None:
    from aicg.bootstrap import CurriculumPlanError, execute_curriculum_plan

    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    with pytest.raises(CurriculumPlanError):
        execute_curriculum_plan(
            manifest=manifest,
            workspace=tmp_path / "workspace",
            role_id="data-engineer",
        )


def test_bootstrap_report_written_to_state_dir(tmp_path: Path) -> None:
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"

    bootstrap_role(
        manifest=manifest,
        workspace=workspace,
        role_id="data-engineer",
        title="Data Engineer",
        level=25,
        state_dir=state_dir,
    )

    report = state_dir / "bootstrap-report.json"
    assert report.exists()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["operation"] == "bootstrap_role"
    assert payload["plan"]["role_id"] == "data-engineer"


def test_execute_plan_scaffolds_single_repo_roles_without_a_solutions_repo(tmp_path: Path) -> None:
    # Startup roles keep everything in one `*-curriculum` repo; scaffolding
    # used to crash on `workspace / None` for the missing solutions repo.
    from aicg.bootstrap import execute_curriculum_plan

    manifest_path = write_minimal_manifest(tmp_path / "aicg-org.yaml")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["roles"].append({
        "id": "data-engineer", "title": "Data Engineer", "level": 25,
        "learning_repo": "data-engineer-curriculum", "solution_repo": None,
        "repo_model": "single",
    })
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    manifest = load_manifest(manifest_path)
    workspace = tmp_path / "workspace"
    learning = workspace / "data-engineer-curriculum"
    plan_path = learning / ".aicg" / "curriculum-plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(json.dumps(_curriculum_plan()), encoding="utf-8")

    report = execute_curriculum_plan(
        manifest=manifest, workspace=workspace, role_id="data-engineer",
        state_dir=tmp_path / "state",
    )

    assert report["modules_created"] == ["mod-101-foundations", "mod-102-streaming"]
    assert report["projects_created"] == ["project-101-batch-pipeline"]
    assert report["solution_path"] is None
    assert (learning / "lessons" / "mod-101-foundations" / "exercises" / "exercise-01-intro.md").exists()
    assert (learning / "projects" / "project-101-batch-pipeline" / "README.md").exists()
    assert sorted(p.name for p in workspace.iterdir()) == ["data-engineer-curriculum"]
