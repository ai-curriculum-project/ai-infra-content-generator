"""Structural audit for learning repos.

The existing :mod:`aicg.audit` is bidirectional and detects *parity*
gaps (where solutions don't match learning). This module is the
companion: it walks a learning repo on its own and surfaces gaps in
the learning content itself — missing module READMEs, empty lecture
notes, exercise files with placeholder bodies, modules without any
exercises.

Findings become ``learning_gap`` work items that the daily-remediate
loop can address. Severity ``medium`` by default, so they sit just
below solution structural gaps in the priority queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .depth import MIN_MODULE_WORDS, learning_module_depth
from .state import utc_now, write_json

LEARNING_AUDIT_REPORT = "learning-audit-report.json"

# Required artifacts at the module level. Many learning repos use
# ``modules/`` while a few historical ones use ``lessons/`` — the
# auditor accepts either.
_MODULE_CONTAINER_DIRS = ("modules", "lessons")
_REQUIRED_MODULE_FILES = ("README.md",)
_EXPECTED_MODULE_SUBDIRS = ("exercises", "lecture-notes")
_PLACEHOLDER_MIN_BYTES = 200  # files smaller than this look unfinished
_PLACEHOLDER_MARKERS = (
    "TODO",
    "TBD",
    "Coming soon",
    "Placeholder",
    "<!-- placeholder -->",
)
_SKIP_DIRS = {".git", ".aicg", "_archive", "node_modules", ".venv"}


class LearningAuditError(RuntimeError):
    """Raised when the learning audit cannot proceed."""


@dataclass(frozen=True)
class LearningGap:
    repo: str
    severity: str
    type: str
    path: str
    message: str

    def to_work_item(self) -> dict[str, Any]:
        slug = _slug(f"{self.type}-{self.path}")
        return {
            "id": f"learning-{slug}",
            "repo": self.repo,
            "type": "learning_gap",
            "severity": self.severity,
            "subtype": self.type,
            "path": self.path,
            "title": self.message[:120],
            "details": self.message,
        }


def audit_learning_repo(
    repo_path: Path,
    *,
    write_report: bool = True,
) -> dict[str, Any]:
    """Walk a learning repo and surface structural gaps.

    Returns ``{"gaps": [...], "work_items": [...], "summary": {...}}``.
    """
    repo_path = Path(repo_path).resolve()
    if not repo_path.exists():
        raise LearningAuditError(f"Repo path missing: {repo_path}")

    container = _find_module_container(repo_path)
    gaps: list[LearningGap] = []

    if container is None:
        gaps.append(
            LearningGap(
                repo=repo_path.name,
                severity="error",
                type="missing_module_container",
                path=".",
                message=(
                    "Learning repo has neither a 'modules/' nor a 'lessons/'"
                    " directory; no learning content surface to audit."
                ),
            )
        )
    else:
        gaps.extend(_audit_module_container(repo_path, container))

    work_items = [g.to_work_item() for g in gaps]
    summary = {
        "module_container": str(container.relative_to(repo_path)) if container else None,
        "module_count": _count_modules(container) if container else 0,
        "gap_count": len(gaps),
        "error_count": sum(1 for g in gaps if g.severity == "error"),
        "warning_count": sum(1 for g in gaps if g.severity == "warning"),
    }
    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "learning_audit",
        "repo": repo_path.name,
        "summary": summary,
        "gaps": [g.__dict__ for g in gaps],
        "work_items": work_items,
    }
    if write_report:
        (repo_path / ".aicg").mkdir(parents=True, exist_ok=True)
        write_json(repo_path / ".aicg" / LEARNING_AUDIT_REPORT, report)
    return report


def _find_module_container(repo_path: Path) -> Path | None:
    for name in _MODULE_CONTAINER_DIRS:
        candidate = repo_path / name
        if candidate.is_dir():
            return candidate
    return None


def _count_modules(container: Path) -> int:
    return sum(
        1
        for entry in container.iterdir()
        if entry.is_dir() and entry.name.startswith("mod-")
    )


def _audit_module_container(repo_path: Path, container: Path) -> list[LearningGap]:
    gaps: list[LearningGap] = []
    module_dirs = [
        d
        for d in sorted(container.iterdir())
        if d.is_dir() and d.name.startswith("mod-") and d.name not in _SKIP_DIRS
    ]
    if not module_dirs:
        gaps.append(
            LearningGap(
                repo=repo_path.name,
                severity="error",
                type="empty_module_container",
                path=str(container.relative_to(repo_path)),
                message=(
                    f"{container.relative_to(repo_path)} contains no mod-*"
                    " directories."
                ),
            )
        )
        return gaps
    for module_dir in module_dirs:
        gaps.extend(_audit_module(repo_path, module_dir))
    return gaps


def _audit_module(repo_path: Path, module_dir: Path) -> list[LearningGap]:
    rel = module_dir.relative_to(repo_path)
    depth = learning_module_depth(module_dir)
    if depth.status != "complete" and (module_dir / "README.md").exists():
        # A skeleton: every file exists but the module was never written. One
        # module-level item covers it (no per-file placeholder duplicates).
        return [
            LearningGap(
                repo=repo_path.name,
                severity="error",
                type="thin_module",
                path=f"{rel}/README.md",
                message=(
                    f"Module {module_dir.name} is a skeleton ({depth.words} words, "
                    f"{depth.chapters} chapters, {depth.exercises} exercises); author the "
                    f"full module (at least {MIN_MODULE_WORDS} words)."
                ),
            )
        ]
    gaps: list[LearningGap] = []

    # Required files.
    for filename in _REQUIRED_MODULE_FILES:
        path = module_dir / filename
        if not path.exists():
            gaps.append(
                LearningGap(
                    repo=repo_path.parent.name if not (repo_path.name) else repo_path.name,
                    severity="error",
                    type="missing_module_readme",
                    path=f"{rel}/{filename}",
                    message=(
                        f"Module {module_dir.name} is missing required file "
                        f"`{filename}`."
                    ),
                )
            )
        elif _looks_placeholder(path):
            gaps.append(
                LearningGap(
                    repo=repo_path.name,
                    severity="warning",
                    type="placeholder_module_readme",
                    path=f"{rel}/{filename}",
                    message=(
                        f"`{rel}/{filename}` looks like a placeholder "
                        f"(<{_PLACEHOLDER_MIN_BYTES} bytes or contains TODO/TBD/etc)."
                    ),
                )
            )

    # Subdirectories we expect to find content in.
    has_exercise_evidence = (
        (module_dir / "exercises").is_dir()
        or any(child.name.startswith("exercise-") for child in module_dir.iterdir())
    )
    if not has_exercise_evidence:
        gaps.append(
            LearningGap(
                repo=repo_path.name,
                severity="warning",
                type="missing_exercises",
                path=str(rel),
                message=(
                    f"Module {module_dir.name} has no 'exercises/' directory "
                    "and no inline exercise-* files."
                ),
            )
        )

    # Lecture notes: warning only — some modules legitimately lack them.
    lecture_notes_dir = module_dir / "lecture-notes"
    if lecture_notes_dir.is_dir():
        notes = [
            p
            for p in lecture_notes_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".md", ".mdx"}
        ]
        if not notes:
            gaps.append(
                LearningGap(
                    repo=repo_path.name,
                    severity="warning",
                    type="empty_lecture_notes",
                    path=str(lecture_notes_dir.relative_to(repo_path)),
                    message=(
                        f"`{lecture_notes_dir.relative_to(repo_path)}/` directory "
                        "exists but is empty."
                    ),
                )
            )

    # Per-exercise placeholder scan inside exercises/ if present.
    exercises_dir = module_dir / "exercises"
    if exercises_dir.is_dir():
        for exercise_path in sorted(exercises_dir.iterdir()):
            if exercise_path.is_file() and exercise_path.suffix.lower() in {".md", ".mdx"}:
                if _looks_placeholder(exercise_path):
                    gaps.append(
                        LearningGap(
                            repo=repo_path.name,
                            severity="warning",
                            type="placeholder_exercise",
                            path=str(exercise_path.relative_to(repo_path)),
                            message=(
                                f"Exercise `{exercise_path.relative_to(repo_path)}` "
                                "looks unfinished."
                            ),
                        )
                    )
    return gaps


def _looks_placeholder(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if len(text.encode("utf-8")) < _PLACEHOLDER_MIN_BYTES:
        return True
    lowered = text.lower()
    hits = sum(1 for marker in _PLACEHOLDER_MARKERS if marker.lower() in lowered)
    return hits >= 2  # two or more placeholder markers


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:120]
