"""Cross-repo pairing audit between paired learning + solution repos.

For each role in the manifest we compare the learning repo's module
and exercise inventory to the solution repo's. Mismatches surface as
``pairing_mismatch`` work items so the daily-remediate loop can
correct them. Catches:

- module ids present in one side but not the other
- exercise slugs that drifted between paired repos
- projects in learning but not in solutions (or vice versa)

The audit is read-only and does not require either repo to be
"complete" on its own — it's strictly about alignment between the
pair.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .org_config import OrgManifest, RoleConfig, state_dir_for_manifest
from .state import utc_now, write_json

PAIRING_AUDIT_REPORT = "pairing-audit-report.json"

_MODULE_DIR_RE = re.compile(r"^mod-\d+")
_EXERCISE_DIR_RE = re.compile(r"^exercise-\d+")
_EXERCISE_FILE_RE = re.compile(r"^exercise-(\d+)[-.]?")
_PROJECT_DIR_RE = re.compile(r"^project-\d+")


class PairingAuditError(RuntimeError):
    """Raised when the pairing audit cannot proceed."""


@dataclass(frozen=True)
class PairingFinding:
    role: str
    severity: str
    type: str
    learning_path: str | None
    solution_path: str | None
    message: str

    def to_work_item(self) -> dict[str, Any]:
        identifier = self.learning_path or self.solution_path or self.type
        slug = _slug(f"{self.role}-{self.type}-{identifier}")
        return {
            "id": f"pairing-{slug}",
            "repo": "_pairing",  # not tied to a single repo
            "role": self.role,
            "type": "pairing_mismatch",
            "severity": self.severity,
            "subtype": self.type,
            "learning_path": self.learning_path,
            "solution_path": self.solution_path,
            "title": self.message[:120],
            "details": self.message,
        }


def audit_pairing(
    manifest: OrgManifest,
    workspace: Path,
    state_dir: Path | None = None,
    write_report: bool = True,
) -> dict[str, Any]:
    """Audit every role's learning + solutions pair for alignment."""
    workspace = Path(workspace).resolve()
    state_root = state_dir_for_manifest(manifest, state_dir)
    state_root.mkdir(parents=True, exist_ok=True)

    role_reports: list[dict[str, Any]] = []
    all_findings: list[PairingFinding] = []
    for role in sorted(manifest.roles, key=lambda r: r.level):
        if role.is_single or not role.solution_repo:
            # Single-repo curricula have no learning/solutions pair to reconcile.
            role_reports.append(
                {
                    "role": role.id,
                    "status": "skipped",
                    "reason": "single-repo curriculum (no pair to audit)",
                }
            )
            continue
        learning_path = workspace / role.learning_repo
        solution_path = workspace / role.solution_repo
        if not learning_path.exists() or not solution_path.exists():
            role_reports.append(
                {
                    "role": role.id,
                    "status": "skipped",
                    "reason": "one or both repos missing",
                    "learning_present": learning_path.exists(),
                    "solution_present": solution_path.exists(),
                }
            )
            continue
        findings = _audit_role_pair(role, learning_path, solution_path)
        all_findings.extend(findings)
        role_reports.append(
            {
                "role": role.id,
                "status": "ok" if not findings else "mismatch",
                "finding_count": len(findings),
            }
        )

    work_items = [f.to_work_item() for f in all_findings]
    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "pairing_audit",
        "role_count": len(role_reports),
        "finding_count": len(all_findings),
        "by_severity": {
            "error": sum(1 for f in all_findings if f.severity == "error"),
            "warning": sum(1 for f in all_findings if f.severity == "warning"),
        },
        "roles": role_reports,
        "findings": [f.__dict__ for f in all_findings],
        "work_items": work_items,
    }
    if write_report:
        write_json(state_root / PAIRING_AUDIT_REPORT, report)
    return report


def _audit_role_pair(
    role: RoleConfig, learning_path: Path, solution_path: Path
) -> list[PairingFinding]:
    findings: list[PairingFinding] = []

    learning_modules = _inventory_modules(learning_path)
    solution_modules = _inventory_modules(solution_path)

    only_in_solutions = sorted(set(solution_modules) - set(learning_modules))
    in_both = sorted(set(learning_modules) & set(solution_modules))

    # NOTE: 'module_only_in_learning' is intentionally OMITTED — the
    # structural solution audit already emits module_solution_gap work
    # items for every learning module that has no solution counterpart.
    for mod_id in only_in_solutions:
        findings.append(
            PairingFinding(
                role=role.id,
                severity="warning",
                type="module_only_in_solutions",
                learning_path=None,
                solution_path=str(solution_modules[mod_id].relative_to(solution_path)),
                message=(
                    f"Module `{mod_id}` exists in solutions repo but is "
                    f"missing from learning."
                ),
            )
        )

    # Exercise-slug drift per paired module.
    # NOTE: 'exercise_missing_in_solutions' is intentionally OMITTED —
    # the structural solution audit already emits module_solution_gap
    # work items whose actions cover the same exercises. Surfacing them
    # here too would double the work.
    for mod_id in in_both:
        l_ex = _inventory_exercises(learning_modules[mod_id])
        s_ex = _inventory_exercises(solution_modules[mod_id])
        for num, l_slug in l_ex.items():
            s_slug = s_ex.get(num)
            if s_slug is None:
                # Covered by the structural audit — skip.
                continue
            if s_slug != l_slug:
                findings.append(
                    PairingFinding(
                        role=role.id,
                        severity="warning",
                        type="exercise_slug_drift",
                        learning_path=f"{mod_id}/exercise-{num:02d}-{l_slug}",
                        solution_path=f"{mod_id}/exercise-{num:02d}-{s_slug}",
                        message=(
                            f"Exercise {num:02d} in `{mod_id}` has different "
                            f"slugs: learning=`{l_slug}` solutions=`{s_slug}`."
                        ),
                    )
                )
        for num, s_slug in s_ex.items():
            if num not in l_ex:
                findings.append(
                    PairingFinding(
                        role=role.id,
                        severity="warning",
                        type="exercise_missing_in_learning",
                        learning_path=None,
                        solution_path=f"{mod_id}/exercise-{num:02d}-{s_slug}",
                        message=(
                            f"Exercise {num:02d} (`{s_slug}`) in `{mod_id}` "
                            "exists in solutions but has no learning equivalent."
                        ),
                    )
                )

    # Project pairing.
    # NOTE: 'project_only_in_learning' is intentionally OMITTED —
    # the structural solution audit already emits project_solution_gap
    # work items for these.
    learning_projects = _inventory_projects(learning_path)
    solution_projects = _inventory_projects(solution_path)
    for proj_id in sorted(set(solution_projects) - set(learning_projects)):
        findings.append(
            PairingFinding(
                role=role.id,
                severity="warning",
                type="project_only_in_solutions",
                learning_path=None,
                solution_path=str(solution_projects[proj_id].relative_to(solution_path)),
                message=(
                    f"Project `{proj_id}` exists in solutions repo but has no "
                    "learning brief."
                ),
            )
        )
    return findings


def _inventory_modules(repo_path: Path) -> dict[str, Path]:
    container = None
    for name in ("modules", "lessons"):
        candidate = repo_path / name
        if candidate.is_dir():
            container = candidate
            break
    if container is None:
        return {}
    return {
        d.name: d
        for d in container.iterdir()
        if d.is_dir() and _MODULE_DIR_RE.match(d.name)
    }


def _inventory_exercises(module_dir: Path) -> dict[int, str]:
    """Return ``{exercise_number: slug}`` for both file- and dir-style exercises."""
    out: dict[int, str] = {}

    # Directory-style: modules/mod-001/exercise-01-foo/
    for child in module_dir.iterdir():
        if not child.is_dir():
            continue
        m = re.match(r"^exercise-(\d+)-(.+)$", child.name)
        if m:
            out[int(m.group(1))] = m.group(2)

    # File-style: modules/mod-001/exercises/exercise-01-foo.md
    exercises_dir = module_dir / "exercises"
    if exercises_dir.is_dir():
        for child in exercises_dir.iterdir():
            if not child.is_file() or child.suffix.lower() not in {".md", ".mdx"}:
                continue
            m = re.match(r"^exercise-(\d+)-(.+)\.(?:md|mdx)$", child.name)
            if m:
                # File-style takes precedence over dir-style if both exist.
                out[int(m.group(1))] = m.group(2)
    return out


def _inventory_projects(repo_path: Path) -> dict[str, Path]:
    container = repo_path / "projects"
    if not container.is_dir():
        return {}
    return {
        d.name: d
        for d in container.iterdir()
        if d.is_dir() and _PROJECT_DIR_RE.match(d.name)
    }


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:120]
