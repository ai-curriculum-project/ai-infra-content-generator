"""Depth checks: telling real content from scaffolding."""

from __future__ import annotations

from pathlib import Path

from aicg.depth import (
    MIN_MODULE_WORDS,
    exercise_solution_is_stub,
    learning_module_depth,
    text_words,
)

STUB = (
    "# mod-101/exercise-02 (Bayes) — Solution\n\n"
    "> Scaffolded by `aicg org execute-plan`. The reference solution lands here on the next "
    "autonomous cycle.\n"
)


def _w(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_text_words_counts_code_files_and_skips_binaries(tmp_path: Path):
    _w(tmp_path / "main.tf", "resource aws_s3_bucket b { bucket = name }")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    _w(tmp_path / ".git" / "HEAD", "ref: refs/heads/main words words words")
    assert text_words(tmp_path) == 8


def test_scaffolded_exercise_solution_is_a_stub(tmp_path: Path):
    _w(tmp_path / "README.md", STUB)
    assert exercise_solution_is_stub(tmp_path)


def test_tiny_exercise_solution_is_a_stub(tmp_path: Path):
    _w(tmp_path / "README.md", "# Solution\n\nSee the code.")
    assert exercise_solution_is_stub(tmp_path)


def test_code_heavy_solution_is_not_a_stub(tmp_path: Path):
    _w(tmp_path / "README.md", "# Terraform module library\n\nRun `terraform init` then apply.")
    _w(tmp_path / "main.tf", "variable x {}\n" * 40)
    assert not exercise_solution_is_stub(tmp_path)


def test_scaffold_phrase_with_real_files_is_not_a_stub(tmp_path: Path):
    _w(tmp_path / "README.md", STUB)
    _w(tmp_path / "solution.py", "def solve(x):\n    return x * 2\n" * 30)
    assert not exercise_solution_is_stub(tmp_path)


def test_missing_solution_dir_is_a_stub(tmp_path: Path):
    assert exercise_solution_is_stub(tmp_path / "nope")


def test_single_file_artifact_is_checked(tmp_path: Path):
    assert exercise_solution_is_stub(_w(tmp_path / "SOLUTION.md", STUB))


def test_skeleton_module_is_thin(tmp_path: Path):
    mod = tmp_path / "mod-403-governance"
    _w(mod / "README.md", "# MOD-403\n\n" + "overview words " * 300)
    _w(mod / "lecture-notes" / "01-overview.md", "short overview")
    _w(mod / "exercises" / "exercise-01-case.md", "Do a case study.")
    depth = learning_module_depth(mod)
    assert depth.status == "thin"
    assert depth.exercises == 1
    assert depth.chapters == 1
    assert not depth.has_quiz


def test_full_module_is_complete(tmp_path: Path):
    mod = tmp_path / "mod-101"
    _w(mod / "README.md", "# Mod\n")
    for i in range(4):
        _w(mod / f"0{i}-chapter.md", "lecture text " * (MIN_MODULE_WORDS // 6))
    _w(mod / "exercises" / "exercise-01-a.md", "brief " * 50)
    _w(mod / "quiz.md", "q " * 20)
    depth = learning_module_depth(mod)
    assert depth.status == "complete"
    assert depth.chapters == 4
    assert depth.has_quiz


def test_empty_module_dir(tmp_path: Path):
    (tmp_path / "mod-1").mkdir()
    assert learning_module_depth(tmp_path / "mod-1").status == "empty"


# --- learning audit + queue priority -------------------------------------------


def test_learning_audit_flags_skeleton_module_once(tmp_path: Path):
    from aicg.learning_audit import audit_learning_repo

    repo = tmp_path / "x-learning"
    mod = repo / "lessons" / "mod-403-governance"
    _w(mod / "README.md", "# MOD-403\n\n" + "overview " * 300)
    _w(mod / "lecture-notes" / "01-overview.md", "short")
    _w(mod / "exercises" / "exercise-01-case.md", "tiny")
    report = audit_learning_repo(repo, write_report=False)
    thin = [g for g in report["gaps"] if g["type"] == "thin_module"]
    assert len(thin) == 1 and thin[0]["path"] == "lessons/mod-403-governance/README.md"
    # The module-level item covers its files; no per-exercise duplicates.
    assert not [g for g in report["gaps"] if g["type"] == "placeholder_exercise"]
    item = next(w for w in report["work_items"] if w["subtype"] == "thin_module")
    assert item["type"] == "learning_gap"


def test_learning_audit_leaves_complete_modules_alone(tmp_path: Path):
    from aicg.learning_audit import audit_learning_repo

    repo = tmp_path / "x-learning"
    mod = repo / "lessons" / "mod-101"
    _w(mod / "README.md", "# Mod\n\n" + "real lecture text " * (MIN_MODULE_WORDS // 3))
    _w(mod / "exercises" / "exercise-01-a.md", "a proper brief " * 40)
    report = audit_learning_repo(repo, write_report=False)
    assert not [g for g in report["gaps"] if g["type"] == "thin_module"]


def _priority_manifest(tmp_path: Path):
    import json

    from aicg.org_config import load_manifest

    path = tmp_path / "m.json"
    path.write_text(json.dumps({
        "org": "o", "default_remote": "git@github.com:o/{repo}.git",
        "roles": [
            {"id": "jr", "title": "Jr", "level": 10, "learning_repo": "jr-learning", "solution_repo": "jr-solutions"},
            {"id": "sr", "title": "Sr", "level": 50, "learning_repo": "sr-learning", "solution_repo": "sr-solutions"},
        ],
    }), encoding="utf-8")
    return load_manifest(path)


def test_queue_puts_skeleton_learning_before_same_role_solutions(tmp_path: Path):
    from aicg.org_runner import queue_priority

    m = _priority_manifest(tmp_path)
    thin = {"type": "learning_gap", "subtype": "thin_module", "severity": "error", "priority": 100}
    sol = {"type": "module_solution_gap", "priority": 1}
    assert queue_priority(m, "jr-learning", thin) < queue_priority(m, "jr-solutions", sol)


def test_queue_orders_entry_roles_first_for_learning_repos(tmp_path: Path):
    from aicg.org_runner import queue_priority

    m = _priority_manifest(tmp_path)
    thin = {"type": "learning_gap", "subtype": "thin_module", "severity": "error", "priority": 100}
    sol = {"type": "module_solution_gap", "priority": 1}
    assert queue_priority(m, "jr-learning", thin) < queue_priority(m, "sr-learning", thin)
    assert queue_priority(m, "jr-solutions", sol) < queue_priority(m, "sr-learning", thin)
