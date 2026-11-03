"""Objective-based curriculum repository audit."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .inventory import RepositoryInfo, WorkspaceInventory
from .depth import exercise_solution_is_stub
from .source_registry import SourceRegistry, extract_urls
from .state import relative_path, utc_now, write_state

AUDIT_REPORT = "audit-report.json"
TEXT_EXTENSIONS = {".md", ".py", ".yaml", ".yml", ".json", ".toml", ".txt", ".sh", ".hcl"}

# Directories we never recurse into when scanning for placeholders or
# auditing source text. ``.aicg`` is excluded because the runner's own
# prompt packets legitimately mention every placeholder word in the
# instructions to the generator. ``_archive`` is excluded because
# several solutions repos park intentional template scaffolds there.
SKIP_DIRS = {
    ".git",
    ".github",
    ".aicg",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "_archive",
    ".scratch-trash",
}

PLACEHOLDER_PATTERNS = {
    "needs_research": re.compile(r"needs-research", re.IGNORECASE),
    "manual_review": re.compile(r"#\s*manual-review|manual-review", re.IGNORECASE),
    "todo": re.compile(r"\bTODO\b|FIXME|\bTBD\b", re.IGNORECASE),
    "placeholder": re.compile(r"\bplaceholder\b|coming soon|lorem ipsum", re.IGNORECASE),
}
# Learning repos name exercises ``exercise-NN-*``; one (senior-engineer) uses
# ``lab-NN-*``. Both pair with ``exercise-NN`` on the solutions side.
EXERCISE_ID_PATTERN = re.compile(r"^((?:exercise|lab)-\d+)")

# Acceptable exercise-level solution artifacts. Any one of these inside
# an exercise directory satisfies the per-exercise solution check.
DEFAULT_EXERCISE_ARTIFACTS: tuple[str, ...] = (
    "SOLUTION.md",
    "STEP_BY_STEP.md",
    "README.md",
)

# Module-level rationale artifact. When this exists at
# ``modules/<module>/SOLUTION.md`` the module is considered to have
# explanatory coverage even if per-exercise artifacts are not present.
MODULE_LEVEL_ARTIFACT = "SOLUTION.md"


class AuditError(RuntimeError):
    """Raised when an audit cannot be completed."""


def audit_repo(
    workspace: Path,
    repo_name: str,
    module: str | None = None,
    write_report: bool = True,
    source_registry: SourceRegistry | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    inventory = WorkspaceInventory(workspace)
    target = inventory.require(repo_name)
    paired = inventory.paired_repo(target)
    registry = source_registry or SourceRegistry.load()

    if target.kind == "solutions" and paired is None:
        raise AuditError(f"Solutions repo '{target.name}' has no paired learning repo in {workspace}.")

    learning_repo = paired if target.kind == "solutions" else target
    solution_repo = target if target.kind == "solutions" else paired

    if learning_repo is None:
        raise AuditError(f"Learning repo for '{target.name}' could not be resolved.")

    modules = audit_learning_solution_parity(
        learning_repo=learning_repo,
        solution_repo=solution_repo,
        module_filter=module,
        registry=registry,
    )
    projects = audit_project_parity(
        learning_repo=learning_repo,
        solution_repo=solution_repo,
    )
    cache = PlaceholderCache(target.path) if use_cache else None
    placeholder_findings = scan_placeholders(target.path, cache=cache)
    if cache is not None:
        cache.save()
    repo_checks = audit_repo_checks(target)

    gaps: list[dict[str, Any]] = []
    for item in modules:
        gaps.extend(item["gaps"])
    for item in projects:
        gaps.extend(item["gaps"])
    gaps.extend(placeholder_findings)
    gaps.extend(repo_checks["gaps"])

    error_count = sum(1 for gap in gaps if gap["severity"] == "error")
    warning_count = sum(1 for gap in gaps if gap["severity"] == "warning")
    report = {
        "schema_version": 2,
        "generated_at": utc_now(),
        "workspace": str(Path(workspace).resolve()),
        "repo": repo_dict(target),
        "paired_repo": repo_dict(paired) if paired else None,
        "source_policy": {
            "registry_last_verified": registry.last_verified,
            "official_first": True,
            "practitioner_reference_note": (
                "VeriSwarm sources may be used only as practitioner implementation references, "
                "not standards authorities."
            ),
        },
        "summary": {
            "status": "fail" if error_count else "pass",
            "error_count": error_count,
            "warning_count": warning_count,
            "module_count": len(modules),
            "project_count": len(projects),
            "gap_count": len(gaps),
        },
        "checks": {
            "learning_solution_parity": {
                "status": "fail" if any(module_item["status"] != "ok" for module_item in modules) else "pass"
            },
            "project_parity": {
                "status": "fail" if any(project["status"] != "ok" for project in projects) else "pass",
                "project_count": len(projects),
            },
            "placeholder_scan": {
                "status": "fail"
                if any(gap["severity"] == "error" for gap in placeholder_findings)
                else "pass",
                "finding_count": len(placeholder_findings),
            },
            "repo_structure": repo_checks["summary"],
        },
        "modules": modules,
        "projects": projects,
        "gaps": gaps,
    }

    if write_report:
        write_state(target.path, AUDIT_REPORT, report)
    return report


def audit_learning_solution_parity(
    learning_repo: RepositoryInfo,
    solution_repo: RepositoryInfo | None,
    module_filter: str | None,
    registry: SourceRegistry,
) -> list[dict[str, Any]]:
    modules: list[dict[str, Any]] = []
    for learning_module in discover_learning_modules(learning_repo):
        module_id = learning_module.name
        if module_filter and module_filter != module_id:
            continue

        solution_module = solution_repo.path / "modules" / module_id if solution_repo else None
        module_rationale = solution_module / MODULE_LEVEL_ARTIFACT if solution_module else None
        has_module_rationale = bool(module_rationale and module_rationale.exists())

        exercises: list[dict[str, Any]] = []
        module_gaps: list[dict[str, Any]] = []

        if solution_repo is None:
            module_gaps.append(
                gap(
                    "missing_paired_solutions_repo",
                    "error",
                    f"Learning module {module_id} has no paired solutions repo.",
                    module_id=module_id,
                    path=relative_path(learning_module, learning_repo.path),
                )
            )
        elif solution_module is None or not solution_module.exists():
            module_gaps.append(
                gap(
                    "missing_solution_module",
                    "error",
                    f"Missing solution module directory for {module_id}.",
                    module_id=module_id,
                    expected_path=relative_path(solution_module, solution_repo.path)
                    if solution_module
                    else None,
                )
            )

        for exercise_entry in discover_exercise_entries(learning_module):
            exercise_id = normalize_exercise_id(exercise_entry)
            slug = exercise_slug(exercise_entry)
            solution_dir = find_exercise_solution_dir(solution_module, exercise_id) if solution_module else None
            found_artifact = find_first_artifact(solution_dir, DEFAULT_EXERCISE_ARTIFACTS) if solution_dir else None

            exercise_status = "ok"
            exercise_gaps: list[dict[str, Any]] = []
            # Scaffolding writes a placeholder README for every exercise, so an
            # artifact existing isn't enough: a stub is treated as missing.
            is_stub = found_artifact is not None and exercise_solution_is_stub(
                solution_dir or found_artifact
            )

            if is_stub:
                exercise_status = "missing_solution"
                exercise_gaps.append(
                    gap(
                        "missing_exercise_solution",
                        "error",
                        f"Solution for {module_id}/{exercise_id} is a scaffold stub, not a solution.",
                        module_id=module_id,
                        exercise_id=exercise_id,
                        learning_path=relative_path(exercise_entry, learning_repo.path),
                        expected_path=relative_path(found_artifact, solution_repo.path),
                        accepted_artifacts=list(DEFAULT_EXERCISE_ARTIFACTS),
                        stub=True,
                    )
                )
            elif found_artifact is None:
                if has_module_rationale:
                    exercise_status = "module_rationale_only"
                    exercise_gaps.append(
                        gap(
                            "exercise_solution_module_level_only",
                            "warning",
                            (
                                f"{module_id}/{exercise_id} relies on module-level rationale "
                                "instead of an exercise-level artifact."
                            ),
                            module_id=module_id,
                            exercise_id=exercise_id,
                            learning_path=relative_path(exercise_entry, learning_repo.path),
                            module_rationale_path=relative_path(module_rationale, solution_repo.path)
                            if module_rationale and solution_repo
                            else None,
                        )
                    )
                else:
                    exercise_status = "missing_solution"
                    expected_dir_path = (
                        relative_path(solution_module / f"{exercise_id}-{slug}" if slug else solution_module / exercise_id, solution_repo.path)
                        if solution_module and solution_repo
                        else None
                    )
                    exercise_gaps.append(
                        gap(
                            "missing_exercise_solution",
                            "error",
                            f"Missing solution artifact for {module_id}/{exercise_id}.",
                            module_id=module_id,
                            exercise_id=exercise_id,
                            learning_path=relative_path(exercise_entry, learning_repo.path),
                            expected_path=expected_dir_path,
                            accepted_artifacts=list(DEFAULT_EXERCISE_ARTIFACTS),
                        )
                    )
            else:
                source_gaps = audit_solution_sources(
                    found_artifact,
                    solution_repo.path,
                    module_id,
                    exercise_id,
                    registry,
                )
                if any(item["severity"] == "error" for item in source_gaps):
                    exercise_status = "source_gap"
                exercise_gaps.extend(source_gaps)

            exercise_title = extract_title(exercise_entry)
            exercises.append(
                {
                    "exercise_id": exercise_id,
                    "slug": slug,
                    "title": exercise_title,
                    "learning_path": relative_path(exercise_entry, learning_repo.path),
                    "expected_solution_dir": relative_path(solution_dir, solution_repo.path)
                    if solution_dir and solution_repo
                    else (
                        relative_path(
                            solution_module / f"{exercise_id}-{slug}" if slug else solution_module / exercise_id,
                            solution_repo.path,
                        )
                        if solution_module and solution_repo
                        else None
                    ),
                    "found_artifact": relative_path(found_artifact, solution_repo.path)
                    if found_artifact and solution_repo
                    else None,
                    "required_artifacts": list(DEFAULT_EXERCISE_ARTIFACTS),
                    "status": exercise_status,
                    "stub": is_stub,
                    "gaps": exercise_gaps,
                }
            )
            module_gaps.extend(exercise_gaps)

        module_status = derive_module_status(
            exercises=exercises,
            has_module_rationale=has_module_rationale,
            module_gaps=module_gaps,
        )
        modules.append(
            {
                "module_id": module_id,
                "learning_path": relative_path(learning_module, learning_repo.path),
                "solution_path": relative_path(solution_module, solution_repo.path)
                if solution_module and solution_repo
                else None,
                "module_rationale_path": relative_path(module_rationale, solution_repo.path)
                if module_rationale and solution_repo and has_module_rationale
                else None,
                "has_module_rationale": has_module_rationale,
                "status": module_status,
                "exercise_count": len(exercises),
                "missing_solution_count": sum(
                    1 for exercise in exercises if exercise["status"] == "missing_solution"
                ),
                "module_rationale_only_count": sum(
                    1 for exercise in exercises if exercise["status"] == "module_rationale_only"
                ),
                "exercises": exercises,
                "gaps": module_gaps,
            }
        )
    return modules


def audit_project_parity(
    learning_repo: RepositoryInfo,
    solution_repo: RepositoryInfo | None,
) -> list[dict[str, Any]]:
    """Check that every learning project has a paired solution project.

    Both sides are conventionally rooted at ``projects/``. A learning
    project at ``projects/project-XXX-name/`` is satisfied by any of
    the standard solution artifacts (SOLUTION.md, STEP_BY_STEP.md,
    README.md) inside the matching solutions repo directory.
    """
    results: list[dict[str, Any]] = []
    learning_projects = discover_projects(learning_repo)
    if not learning_projects:
        return results

    solution_root = solution_repo.path / "projects" if solution_repo else None

    for learning_project in learning_projects:
        project_id = learning_project.name
        project_gaps: list[dict[str, Any]] = []
        solution_dir: Path | None = None
        found_artifact: Path | None = None

        if solution_repo is None or solution_root is None:
            project_gaps.append(
                gap(
                    "missing_paired_solutions_repo",
                    "error",
                    f"Learning project {project_id} has no paired solutions repo.",
                    project_id=project_id,
                    learning_path=relative_path(learning_project, learning_repo.path),
                )
            )
        else:
            solution_dir = find_project_solution_dir(solution_root, project_id)
            if solution_dir is None:
                project_gaps.append(
                    gap(
                        "missing_solution_project",
                        "error",
                        f"Missing solution project directory for {project_id}.",
                        project_id=project_id,
                        learning_path=relative_path(learning_project, learning_repo.path),
                        expected_path=relative_path(
                            solution_root / project_id, solution_repo.path
                        ),
                    )
                )
            else:
                found_artifact = find_first_artifact(
                    solution_dir, DEFAULT_EXERCISE_ARTIFACTS
                )
                if found_artifact is None:
                    project_gaps.append(
                        gap(
                            "missing_project_solution_artifact",
                            "error",
                            f"Solution project {project_id} has no SOLUTION.md / README.md / STEP_BY_STEP.md.",
                            project_id=project_id,
                            expected_path=relative_path(solution_dir, solution_repo.path),
                            accepted_artifacts=list(DEFAULT_EXERCISE_ARTIFACTS),
                        )
                    )

        status = "ok" if not project_gaps else "gap"
        results.append(
            {
                "project_id": project_id,
                "title": extract_title(learning_project),
                "learning_path": relative_path(learning_project, learning_repo.path),
                "solution_path": relative_path(solution_dir, solution_repo.path)
                if solution_dir and solution_repo
                else None,
                "found_artifact": relative_path(found_artifact, solution_repo.path)
                if found_artifact and solution_repo
                else None,
                "status": status,
                "gaps": project_gaps,
            }
        )
    return results


def discover_projects(repo: RepositoryInfo) -> list[Path]:
    root = repo.path / "projects"
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and path.name.lower().startswith("project-")
    )


def find_project_solution_dir(projects_root: Path, project_id: str) -> Path | None:
    """Locate the on-disk solution project directory.

    Solution projects normally share the same name as the learning
    project, but some repos prefix differently (``project-301-`` vs
    ``project-1-``). We accept exact match first, then any directory
    whose first hyphen-separated token matches the learning project's
    first token.
    """
    if not projects_root.exists():
        return None
    exact = projects_root / project_id
    if exact.is_dir():
        return exact
    # Fallback: numeric / prefix match. project-101-foo -> project-101*
    parts = project_id.split("-")
    if len(parts) < 2:
        return None
    prefix = "-".join(parts[:2])
    candidates = [
        path
        for path in projects_root.iterdir()
        if path.is_dir() and path.name.startswith(prefix)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def derive_module_status(
    exercises: list[dict[str, Any]],
    has_module_rationale: bool,
    module_gaps: list[dict[str, Any]],
) -> str:
    if any(item["severity"] == "error" for item in module_gaps):
        return "gap"
    if not exercises:
        return "ok" if has_module_rationale else "empty"
    statuses = {exercise["status"] for exercise in exercises}
    if statuses == {"ok"}:
        return "ok"
    if statuses <= {"ok", "module_rationale_only"}:
        return "module_rationale_only"
    return "gap"


def discover_learning_modules(repo: RepositoryInfo) -> list[Path]:
    root = repo.module_root
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("mod-"))


def discover_exercise_entries(module_path: Path) -> list[Path]:
    """Yield each entry under ``exercises/`` that names a learning exercise.

    Entries can be either:
        - A markdown file (e.g. ``exercise-01-threat-model.md``).
        - A directory (e.g. ``exercise-04-python-env-manager/``).

    README/index files at the root of ``exercises/`` are excluded.
    """
    exercises_dir = module_path / "exercises"
    if not exercises_dir.exists():
        return []
    entries: list[Path] = []
    for path in sorted(exercises_dir.iterdir()):
        if path.name.lower().startswith("readme"):
            continue
        if path.is_file():
            if path.suffix.lower() == ".md" and EXERCISE_ID_PATTERN.match(path.stem):
                entries.append(path)
        elif path.is_dir():
            if EXERCISE_ID_PATTERN.match(path.name):
                entries.append(path)
    return entries


def normalize_exercise_id(path: Path) -> str:
    """Return the canonical ``exercise-NN`` id for either a file or directory."""
    name = path.stem if path.is_file() else path.name
    match = EXERCISE_ID_PATTERN.match(name)
    if not match:
        return name
    return re.sub(r"^lab-", "exercise-", match.group(1))


def exercise_slug(path: Path) -> str | None:
    """Return the non-numeric suffix portion of the exercise id, if any."""
    name = path.stem if path.is_file() else path.name
    match = EXERCISE_ID_PATTERN.match(name)
    if not match:
        return None
    remainder = name[match.end() :]
    return remainder.lstrip("-") or None


def find_exercise_solution_dir(solution_module: Path, exercise_id: str) -> Path | None:
    """Locate the on-disk solution directory for ``exercise_id``.

    Solutions repos in this curriculum use a few conventions:

    - ``modules/<module>/<exercise-NN-slug>/``  (engineer + senior-engineer)
    - ``modules/<module>/<exercise-NN>/``       (security pilot)

    We accept either, glob-matching on the ``exercise-NN`` prefix.
    """
    if not solution_module.exists():
        return None
    exact = solution_module / exercise_id
    if exact.is_dir():
        return exact
    # Look for sibling directories with the same numeric prefix.
    candidates = [
        path
        for path in solution_module.iterdir()
        if path.is_dir()
        and path.name.startswith(f"{exercise_id}-")
    ]
    if len(candidates) == 1:
        return candidates[0]
    # Multiple matches (rare) — prefer the longest slug match.
    if candidates:
        return sorted(candidates, key=lambda item: -len(item.name))[0]
    return None


def find_first_artifact(directory: Path | None, names: tuple[str, ...]) -> Path | None:
    if directory is None or not directory.is_dir():
        return None
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def extract_title(path: Path) -> str:
    target = path
    if path.is_dir():
        readme = path / "README.md"
        if readme.exists():
            target = readme
        else:
            return path.name.replace("-", " ").title()
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
    except (OSError, UnicodeDecodeError):
        return target.stem
    return target.stem.replace("-", " ").title()


def audit_solution_sources(
    solution_file: Path,
    repo_path: Path,
    module_id: str,
    exercise_id: str,
    registry: SourceRegistry,
) -> list[dict[str, Any]]:
    content = solution_file.read_text(encoding="utf-8")
    urls = extract_urls(content)
    if not urls and len(content.split()) >= 250:
        return [
            gap(
                "missing_source_references",
                "warning",
                f"{module_id}/{exercise_id} has substantial explanation but no source URLs.",
                module_id=module_id,
                exercise_id=exercise_id,
                path=relative_path(solution_file, repo_path),
            )
        ]

    classified = [registry.classify_url(url) for url in urls]
    has_official = any(source and source.is_official for source in classified)
    has_practitioner = any(source and source.is_practitioner_reference for source in classified)
    findings: list[dict[str, Any]] = []
    if has_practitioner and not has_official:
        findings.append(
            gap(
                "practitioner_reference_without_official_source",
                "warning",
                f"{module_id}/{exercise_id} uses practitioner references without an official source.",
                module_id=module_id,
                exercise_id=exercise_id,
                path=relative_path(solution_file, repo_path),
            )
        )
    return findings


def scan_placeholders(
    repo_path: Path,
    cache: "PlaceholderCache | None" = None,
) -> list[dict[str, Any]]:
    if cache is not None:
        return cache.scan(repo_path)
    findings: list[dict[str, Any]] = []
    for path in iter_text_files(repo_path):
        findings.extend(_scan_file_for_placeholders(path, repo_path))
    return findings


def _scan_file_for_placeholders(path: Path, repo_path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    findings: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        for marker, pattern in PLACEHOLDER_PATTERNS.items():
            if pattern.search(line):
                severity = "error" if marker in {"needs_research", "manual_review"} else "warning"
                findings.append(
                    gap(
                        marker,
                        severity,
                        f"Found {marker.replace('_', '-')} marker.",
                        path=relative_path(path, repo_path),
                        line=line_number,
                        excerpt=line.strip()[:160],
                    )
                )
    return findings


def iter_text_files(repo_path: Path) -> list[Path]:
    paths: list[Path] = []
    for path in repo_path.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS:
            paths.append(path)
    return paths


def audit_repo_checks(repo: RepositoryInfo) -> dict[str, Any]:
    gaps: list[dict[str, Any]] = []
    if not (repo.path / ".github" / "workflows").exists():
        gaps.append(
            gap(
                "missing_ci_workflow",
                "warning",
                "Repository has no .github/workflows directory for validation.",
            )
        )
    if repo.kind == "solutions" and not (repo.path / "modules").exists():
        gaps.append(
            gap(
                "missing_modules_dir",
                "error",
                "Solutions repository has no modules directory.",
                expected_path="modules",
            )
        )
    return {
        "summary": {
            "status": "fail" if any(item["severity"] == "error" for item in gaps) else "pass",
            "finding_count": len(gaps),
        },
        "gaps": gaps,
    }


def gap(kind: str, severity: str, message: str, **extra: Any) -> dict[str, Any]:
    data = {"type": kind, "severity": severity, "message": message}
    data.update({key: value for key, value in extra.items() if value is not None})
    return data


def repo_dict(repo: RepositoryInfo | None) -> dict[str, Any] | None:
    if repo is None:
        return None
    return {
        "name": repo.name,
        "path": str(repo.path),
        "kind": repo.kind,
        "track": repo.track,
        "has_git": repo.has_git,
    }


# ---------------------------------------------------------------------------
# Placeholder scan cache
# ---------------------------------------------------------------------------


class PlaceholderCache:
    """Cache placeholder-scan findings by file mtime+size.

    Stored under ``<repo>/.aicg/placeholder-cache.json`` with the shape::

        {
            "schema_version": 1,
            "entries": {
                "<relative path>": {
                    "mtime_ns": int,
                    "size": int,
                    "findings": [...]
                }
            }
        }

    Callers should construct one instance per repo, call ``scan(repo_path)``
    once, and then ``save()`` to persist updates.
    """

    SCHEMA_VERSION = 1

    def __init__(self, repo_path: Path):
        from .state import ensure_state_dir

        self._repo_path = repo_path.resolve()
        state_dir = ensure_state_dir(self._repo_path)
        self._cache_path = state_dir / "placeholder-cache.json"
        self._entries: dict[str, dict[str, Any]] = self._load_entries()
        self._dirty = False

    def _load_entries(self) -> dict[str, dict[str, Any]]:
        if not self._cache_path.exists():
            return {}
        try:
            import json

            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if int(payload.get("schema_version", 0)) != self.SCHEMA_VERSION:
                return {}
            entries = payload.get("entries")
            return entries if isinstance(entries, dict) else {}
        except (OSError, ValueError):
            return {}

    def scan(self, repo_path: Path) -> list[dict[str, Any]]:
        repo_path = repo_path.resolve()
        if repo_path != self._repo_path:
            # Caller mismatch — fall back to non-cached scan to stay
            # correct.
            return scan_placeholders(repo_path)

        findings: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in iter_text_files(repo_path):
            relative = relative_path(path, repo_path)
            seen.add(relative)
            try:
                stat = path.stat()
            except OSError:
                continue
            entry = self._entries.get(relative)
            if (
                entry
                and entry.get("mtime_ns") == stat.st_mtime_ns
                and entry.get("size") == stat.st_size
            ):
                findings.extend(entry.get("findings", []))
                continue
            file_findings = _scan_file_for_placeholders(path, repo_path)
            self._entries[relative] = {
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "findings": file_findings,
            }
            self._dirty = True
            findings.extend(file_findings)

        # Drop cache rows for files that no longer exist.
        stale = [key for key in self._entries if key not in seen]
        if stale:
            for key in stale:
                self._entries.pop(key, None)
            self._dirty = True

        return findings

    def save(self) -> Path | None:
        if not self._dirty:
            return None
        from .state import write_json

        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "generated_at": utc_now(),
            "entries": self._entries,
        }
        write_json(self._cache_path, payload)
        self._dirty = False
        return self._cache_path
