"""Calculated completeness: per module, per role, per org, and across the family."""

from __future__ import annotations

import json
from pathlib import Path

from aicg.completeness import (
    MATRIX_FILE,
    measure_role,
    render_family_matrix,
    render_org_matrix,
    render_role_matrix,
    same_except_date,
    write_matrices,
)
from aicg.org_config import load_manifest

LECTURE = "Real lecture prose about the topic. " * 700  # ~4,200 words
SOLUTION = "A worked solution walkthrough with code and reasoning. " * 20
STUB = "# Solution\n\n> Scaffolded by `aicg org execute-plan`. The reference solution lands here on the next autonomous cycle.\n"


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _manifest(tmp_path: Path, roles: list[dict] | None = None, org: str = "demo-curriculum"):
    path = tmp_path / f"{org}.json"
    path.write_text(json.dumps({
        "org": org,
        "default_remote": f"git@github.com:{org}/{{repo}}.git",
        "roles": roles or [
            {"id": "dev", "title": "Developer", "level": 20,
             "learning_repo": "dev-learning", "solution_repo": "dev-solutions"},
            {"id": "lead", "title": "Lead", "level": 40,
             "learning_repo": "lead-learning", "solution_repo": "lead-solutions"},
        ],
    }), encoding="utf-8")
    return load_manifest(path)


def _module(ws: Path, repo: str, mod: str, *, lecture: str, exercises: int) -> None:
    base = ws / repo / "lessons" / mod
    _w(base / "README.md", f"# {mod.title()}\n\n{lecture}")
    for i in range(1, exercises + 1):
        _w(base / "exercises" / f"exercise-0{i}-task.md", "A full exercise brief. " * 40)
    _w(base / "quiz.md", "Q1?")


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    _module(ws, "dev-learning", "mod-101-basics", lecture=LECTURE, exercises=2)
    _module(ws, "dev-learning", "mod-102-skeleton", lecture="outline only", exercises=2)
    sol = ws / "dev-solutions" / "modules"
    _w(sol / "mod-101-basics" / "exercise-01-task" / "README.md", SOLUTION)
    _w(sol / "mod-101-basics" / "exercise-02-task" / "README.md", STUB)
    _w(sol / "mod-102-skeleton" / "exercise-01-task" / "README.md", STUB)
    plan = {"modules": [{"id": "mod-101-basics"}, {"id": "mod-102-skeleton"}, {"id": "mod-103-planned"}]}
    _w(ws / "dev-learning" / ".aicg" / "curriculum-plan.json", json.dumps(plan))
    return ws


def test_measure_role_counts_modules_and_solutions(tmp_path: Path):
    ws = _workspace(tmp_path)
    m = _manifest(tmp_path)
    rc = measure_role(m, m.roles[0], ws)
    statuses = {mod.module_id: mod.status for mod in rc.modules}
    assert statuses == {
        "mod-101-basics": "complete",
        "mod-102-skeleton": "thin",
        "mod-103-planned": "not_started",
    }
    assert rc.modules_complete == 1 and rc.modules_total == 3
    assert rc.exercises_total == 4 and rc.exercises_solved == 1
    assert round(rc.learning_pct) == 33
    assert round(rc.solutions_pct) == 25
    assert round(rc.overall_pct) == 29


def test_role_without_repo_or_plan_is_not_started(tmp_path: Path):
    m = _manifest(tmp_path)
    rc = measure_role(m, m.roles[1], tmp_path / "empty-ws")
    assert rc.modules_total == 0
    assert rc.overall_pct == 0
    assert rc.status_label == "Not started"


def test_single_repo_roles_score_learning_only(tmp_path: Path):
    ws = tmp_path / "ws"
    _module(ws, "found-curriculum", "mod-001-discovery", lecture=LECTURE, exercises=1)
    m = _manifest(tmp_path, roles=[{"id": "found", "title": "Founder", "level": 20,
                                    "learning_repo": "found-curriculum", "solution_repo": None,
                                    "repo_model": "single"}])
    rc = measure_role(m, m.roles[0], ws)
    assert rc.solutions_pct is None
    assert rc.overall_pct == 100


def test_role_matrix_lists_every_module_with_status(tmp_path: Path):
    ws = _workspace(tmp_path)
    m = _manifest(tmp_path)
    text = render_role_matrix(m, measure_role(m, m.roles[0], ws), today="2026-11-03")
    assert text.startswith("# Completeness Matrix: Developer")
    assert "| `mod-102-skeleton` |" in text and "Skeleton" in text
    assert "| `mod-103-planned` |" in text and "Not started" in text
    assert "1/2" in text  # mod-101 solutions
    assert "**Overall: 29%**" in text


def test_org_matrix_has_a_row_per_role_and_totals(tmp_path: Path):
    ws = _workspace(tmp_path)
    m = _manifest(tmp_path)
    roles = [measure_role(m, r, ws) for r in m.roles]
    text = render_org_matrix(m, roles, today="2026-11-03")
    assert "[Developer](https://github.com/demo-curriculum/dev-learning/blob/main/COMPLETENESS_MATRIX.md)" in text
    assert "| **Total** |" in text
    assert "Not started" in text


def test_family_matrix_links_each_org(tmp_path: Path):
    ws = _workspace(tmp_path)
    m = _manifest(tmp_path)
    roles = [measure_role(m, r, ws) for r in m.roles]
    text = render_family_matrix([(m, roles)], today="2026-11-03")
    assert "https://github.com/demo-curriculum/.github/blob/main/COMPLETENESS_MATRIX.md" in text


def test_same_except_date_ignores_only_the_generated_line():
    a = "# T\n\n_Generated 2026-11-03 by `aicg org completeness`._\n\n| x |\n"
    b = "# T\n\n_Generated 2026-11-10 by `aicg org completeness`._\n\n| x |\n"
    assert same_except_date(a, b)
    assert not same_except_date(a, b.replace("| x |", "| y |"))


def test_write_matrices_writes_role_and_org_files_and_skips_date_only_changes(tmp_path: Path):
    ws = _workspace(tmp_path)
    (ws / "dev-solutions").mkdir(exist_ok=True)
    (ws / "demo-curriculum-dotgithub").mkdir()
    m = _manifest(tmp_path)
    first = write_matrices(m, ws, today="2026-11-03")
    assert ws / "dev-learning" / MATRIX_FILE in first
    assert ws / "dev-solutions" / MATRIX_FILE in first
    assert ws / "demo-curriculum-dotgithub" / MATRIX_FILE in first
    assert not (ws / "lead-learning").exists()  # missing repos are skipped, not created
    assert write_matrices(m, ws, today="2026-11-10") == []
