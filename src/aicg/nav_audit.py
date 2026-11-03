"""Navigation-doc completeness audits.

Two related but separate audits live here:

1. :func:`audit_curriculum_nav` — per repo, walks ``CURRICULUM.md`` and
   ``CURRICULUM_INDEX.md`` (when present) and checks every module /
   project actually on disk is referenced, and every referenced item
   actually exists. Surfaces orphan-on-disk and broken-reference gaps.

2. :func:`audit_org_profile` — org-level, audits the ``.github`` repo's
   profile/README.md and README.md for staleness: repo cross-link rot,
   badge URL freshness, missing repos in the list, repos in the list
   that no longer exist in the manifest.

Both emit work items the daily-remediate loop can pick up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .org_config import OrgManifest, state_dir_for_manifest
from .state import utc_now, write_json

CURRICULUM_NAV_REPORT = "curriculum-nav-report.json"
ORG_PROFILE_REPORT = "org-profile-report.json"

_MODULE_DIR_RE = re.compile(r"^mod-\d+")
_PROJECT_DIR_RE = re.compile(r"^project-\d+")
_REPO_LINK_RE = re.compile(r"\[[^\]]+\]\((?:https://github\.com/[^/]+/)?([a-z][a-z0-9-_.]+)\)")


# ---------------------------------------------------------------------------
# CURRICULUM.md / CURRICULUM_INDEX.md completeness
# ---------------------------------------------------------------------------


class CurriculumNavError(RuntimeError):
    pass


@dataclass(frozen=True)
class NavFinding:
    repo: str
    severity: str
    type: str
    path: str
    message: str

    def to_work_item(self) -> dict[str, Any]:
        slug = _slug(f"{self.type}-{self.path}")
        return {
            "id": f"nav-{slug}",
            "repo": self.repo,
            "type": "curriculum_nav_drift",
            "severity": self.severity,
            "subtype": self.type,
            "path": self.path,
            "title": self.message[:120],
            "details": self.message,
        }


def audit_curriculum_nav(
    repo_path: Path,
    *,
    write_report: bool = True,
) -> dict[str, Any]:
    """Check that nav docs reference exactly what exists on disk."""
    repo_path = Path(repo_path).resolve()
    if not repo_path.exists():
        raise CurriculumNavError(f"Repo not found: {repo_path}")

    findings: list[NavFinding] = []
    on_disk_modules = _modules_on_disk(repo_path)
    on_disk_projects = _projects_on_disk(repo_path)

    curric_path = repo_path / "CURRICULUM.md"
    index_path = repo_path / "CURRICULUM_INDEX.md"

    if curric_path.exists():
        referenced = _extract_referenced_paths(curric_path)
        findings.extend(
            _compare_disk_vs_referenced(
                repo_path=repo_path,
                doc_path=curric_path,
                disk_modules=on_disk_modules,
                disk_projects=on_disk_projects,
                referenced=referenced,
            )
        )
    else:
        if on_disk_modules or on_disk_projects:
            findings.append(
                NavFinding(
                    repo=repo_path.name,
                    severity="warning",
                    type="missing_curriculum_md",
                    path="CURRICULUM.md",
                    message=(
                        "Repo has modules/projects but no CURRICULUM.md to "
                        "navigate them."
                    ),
                )
            )

    if index_path.exists():
        referenced = _extract_referenced_paths(index_path)
        findings.extend(
            _compare_disk_vs_referenced(
                repo_path=repo_path,
                doc_path=index_path,
                disk_modules=on_disk_modules,
                disk_projects=on_disk_projects,
                referenced=referenced,
            )
        )

    work_items = [f.to_work_item() for f in findings]
    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "curriculum_nav_audit",
        "repo": repo_path.name,
        "modules_on_disk": sorted(on_disk_modules),
        "projects_on_disk": sorted(on_disk_projects),
        "curriculum_md_present": curric_path.exists(),
        "curriculum_index_present": index_path.exists(),
        "gap_count": len(findings),
        "findings": [f.__dict__ for f in findings],
        "work_items": work_items,
    }
    if write_report:
        (repo_path / ".aicg").mkdir(parents=True, exist_ok=True)
        write_json(repo_path / ".aicg" / CURRICULUM_NAV_REPORT, report)
    return report


def _modules_on_disk(repo_path: Path) -> set[str]:
    out: set[str] = set()
    for container_name in ("modules", "lessons"):
        container = repo_path / container_name
        if not container.is_dir():
            continue
        for child in container.iterdir():
            if child.is_dir() and _MODULE_DIR_RE.match(child.name):
                out.add(child.name)
    return out


def _projects_on_disk(repo_path: Path) -> set[str]:
    container = repo_path / "projects"
    if not container.is_dir():
        return set()
    return {
        child.name
        for child in container.iterdir()
        if child.is_dir() and _PROJECT_DIR_RE.match(child.name)
    }


def _extract_referenced_paths(doc_path: Path) -> set[str]:
    """Pull out module/project ids referenced in markdown links."""
    try:
        text = doc_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    ids: set[str] = set()
    for match in re.finditer(r"`(mod-\d+[-a-z0-9]*)`|`(project-\d+[-a-z0-9]*)`", text):
        ident = match.group(1) or match.group(2)
        if ident:
            ids.add(ident)
    for match in re.finditer(
        r"(modules|lessons|projects)/(mod-\d+[-a-z0-9]*|project-\d+[-a-z0-9]*)",
        text,
    ):
        ids.add(match.group(2))
    return ids


def _compare_disk_vs_referenced(
    repo_path: Path,
    doc_path: Path,
    disk_modules: set[str],
    disk_projects: set[str],
    referenced: set[str],
) -> list[NavFinding]:
    findings: list[NavFinding] = []
    rel = str(doc_path.relative_to(repo_path))

    disk_all = disk_modules | disk_projects
    orphans_on_disk = sorted(disk_all - referenced)
    references_missing_on_disk = sorted(referenced - disk_all)

    for ident in orphans_on_disk:
        kind = "module" if ident.startswith("mod-") else "project"
        findings.append(
            NavFinding(
                repo=repo_path.name,
                severity="warning",
                type="nav_missing_reference",
                path=f"{rel}#{ident}",
                message=(
                    f"`{rel}` does not reference {kind} `{ident}` that exists "
                    "on disk."
                ),
            )
        )
    for ident in references_missing_on_disk:
        kind = "module" if ident.startswith("mod-") else "project"
        findings.append(
            NavFinding(
                repo=repo_path.name,
                severity="warning",
                type="nav_broken_reference",
                path=f"{rel}#{ident}",
                message=(
                    f"`{rel}` references {kind} `{ident}` but no such directory "
                    "exists on disk."
                ),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Org-level profile audit
# ---------------------------------------------------------------------------


class OrgProfileAuditError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProfileFinding:
    severity: str
    type: str
    path: str
    message: str

    def to_work_item(self) -> dict[str, Any]:
        slug = _slug(f"profile-{self.type}-{self.path}")
        return {
            "id": f"org-profile-{slug}",
            "repo": ".github",
            "type": "org_profile_stale",
            "severity": self.severity,
            "subtype": self.type,
            "path": self.path,
            "title": self.message[:120],
            "details": self.message,
        }


def audit_org_profile(
    manifest: OrgManifest,
    workspace: Path,
    state_dir: Path | None = None,
    write_report: bool = True,
) -> dict[str, Any]:
    """Audit the .github org-profile docs for staleness vs the manifest."""
    workspace = Path(workspace).resolve()
    state_root = state_dir_for_manifest(manifest, state_dir)
    state_root.mkdir(parents=True, exist_ok=True)

    profile_repo = manifest.repo_path(workspace, ".github")
    findings: list[ProfileFinding] = []
    docs_to_check = [
        profile_repo / "README.md",
        profile_repo / "profile" / "README.md",
    ]
    if not profile_repo.exists():
        findings.append(
            ProfileFinding(
                severity="warning",
                type="missing_profile_repo",
                path=".github",
                message=(
                    "Manifest references `.github` as the org-profile repo "
                    "but the directory is missing from the workspace."
                ),
            )
        )
    else:
        for doc in docs_to_check:
            if not doc.exists():
                continue
            findings.extend(_audit_profile_doc(doc, manifest, profile_repo))

    work_items = [f.to_work_item() for f in findings]
    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "org_profile_audit",
        "profile_repo": str(profile_repo),
        "gap_count": len(findings),
        "findings": [f.__dict__ for f in findings],
        "work_items": work_items,
    }
    if write_report:
        write_json(state_root / ORG_PROFILE_REPORT, report)
    return report


def _audit_profile_doc(
    doc_path: Path, manifest: OrgManifest, profile_repo: Path
) -> list[ProfileFinding]:
    rel = str(doc_path.relative_to(profile_repo))
    try:
        text = doc_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [
            ProfileFinding(
                severity="error",
                type="unreadable_doc",
                path=rel,
                message=f"Could not read `{rel}`.",
            )
        ]

    findings: list[ProfileFinding] = []
    manifest_repos = set(manifest.repo_names)
    known_refs = manifest.known_org_references

    # Find repo references in the doc and compare against the manifest.
    referenced_repos: set[str] = set()
    for match in _REPO_LINK_RE.finditer(text):
        candidate = match.group(1)
        if candidate in manifest_repos or candidate.startswith("ai-infra-"):
            referenced_repos.add(candidate)

    missing_from_doc = sorted(manifest_repos - referenced_repos)
    # known_org_references (e.g. the project's maintainer) are
    # explicit allow-listed even though they aren't in the manifest's
    # repo_names. Filter them out of the orphan check.
    referenced_but_unknown = sorted(
        ref for ref in (referenced_repos - manifest_repos)
        if ref not in known_refs and ref.lower() not in known_refs
    )

    if missing_from_doc:
        findings.append(
            ProfileFinding(
                severity="warning",
                type="profile_missing_repos",
                path=rel,
                message=(
                    f"`{rel}` does not reference these manifest repos: "
                    + ", ".join(missing_from_doc[:10])
                    + (f" (+{len(missing_from_doc) - 10} more)" if len(missing_from_doc) > 10 else "")
                ),
            )
        )
    if referenced_but_unknown:
        findings.append(
            ProfileFinding(
                severity="warning",
                type="profile_orphan_references",
                path=rel,
                message=(
                    f"`{rel}` references repos not in the manifest: "
                    + ", ".join(referenced_but_unknown[:10])
                ),
            )
        )

    # Empty profile doc is a finding on its own.
    if len(text.strip()) < 200:
        findings.append(
            ProfileFinding(
                severity="warning",
                type="profile_too_thin",
                path=rel,
                message=(
                    f"`{rel}` is less than 200 bytes; org profile likely needs "
                    "real content."
                ),
            )
        )
    return findings


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:120]
