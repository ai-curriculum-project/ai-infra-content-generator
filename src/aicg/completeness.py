"""Calculated completeness, written as COMPLETENESS_MATRIX.md files.

Every role repo gets a per-module matrix; each org's ``.github`` repo gets a
per-role matrix; the parent org gets a per-org one. The numbers come from the
same depth checks the audits use (``aicg.depth``), so the matrix shows what
the pipeline will work on next:

- **Learning %**: complete modules / all modules (the curriculum plan's
  modules plus anything else on disk). Skeleton modules don't count.
- **Solutions %**: exercises with a real (non-stub) solution / all exercises,
  including exercises the plan lists for modules not written yet.
- **Overall %**: the mean of the two (learning only for single-repo curricula).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .audit import discover_exercise_entries, normalize_exercise_id
from .depth import (
    MIN_MODULE_WORDS,
    exercise_solution_is_stub,
    learning_module_depth,
)
from .domain_provision import _org_display

MATRIX_FILE = "COMPLETENESS_MATRIX.md"
_GENERATED_RE = re.compile(r"^_Generated \d{4}-\d{2}-\d{2} by `aicg org completeness`\._$", re.M)
_STATUS_LABEL = {"complete": "✅ Complete", "thin": "🟡 Skeleton", "empty": "🟡 Skeleton",
                 "not_started": "⬜ Not started"}


@dataclass(frozen=True)
class ModuleCompleteness:
    module_id: str
    status: str  # complete | thin | empty | not_started
    words: int = 0
    chapters: int = 0
    exercises: int = 0
    solved: int | None = None  # None for single-repo curricula
    has_quiz: bool = False


@dataclass(frozen=True)
class RoleCompleteness:
    role_id: str
    title: str
    level: int
    learning_repo: str
    solution_repo: str | None
    modules: tuple[ModuleCompleteness, ...] = field(default_factory=tuple)

    @property
    def modules_total(self) -> int:
        return len(self.modules)

    @property
    def modules_complete(self) -> int:
        return sum(1 for m in self.modules if m.status == "complete")

    @property
    def exercises_total(self) -> int:
        return sum(m.exercises for m in self.modules)

    @property
    def exercises_solved(self) -> int:
        return sum(m.solved or 0 for m in self.modules)

    @property
    def learning_pct(self) -> float:
        return 100 * self.modules_complete / self.modules_total if self.modules_total else 0.0

    @property
    def solutions_pct(self) -> float | None:
        """None when there's nothing to solve (no solutions repo, or no exercises yet)."""
        if self.solution_repo is None or (self.exercises_total == 0 and self.modules_complete):
            return None
        return 100 * self.exercises_solved / self.exercises_total if self.exercises_total else 0.0

    @property
    def overall_pct(self) -> float:
        if self.solutions_pct is None:
            return self.learning_pct
        return (self.learning_pct + self.solutions_pct) / 2

    @property
    def status_label(self) -> str:
        if self.modules_total == 0 or self.overall_pct == 0:
            return "Not started"
        return "Complete" if self.overall_pct >= 99.5 else "In progress"


# --- measuring ---------------------------------------------------------------------


def _plan_modules(learning: Path) -> list[dict[str, Any]]:
    path = learning / ".aicg" / "curriculum-plan.json"
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("modules", []))
    except (OSError, ValueError):
        return []


def _module_dirs(repo: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for container in ("lessons", "modules"):
        for d in sorted((repo / container).glob("mod-*")):
            if d.is_dir():
                out.setdefault(d.name, d)
    return out


def _exercise_numbers(module: Path) -> list[str]:
    """Canonical ``exercise-NN`` ids, discovered exactly as the audit does."""
    entries = list(discover_exercise_entries(module))
    entries += [p for p in module.glob("exercise-*") if p.is_dir()]
    return sorted({normalize_exercise_id(p) for p in entries})


def _solved(solution_module: Path, numbers: list[str]) -> int:
    if not solution_module.is_dir():
        return 0
    rationale = solution_module / "SOLUTION.md"
    if rationale.exists() and not exercise_solution_is_stub(rationale):
        return len(numbers)
    solved = 0
    for number in numbers:
        candidates = [p for p in solution_module.iterdir() if re.match(rf"{number}(\b|-|$)", p.name)]
        if any(not exercise_solution_is_stub(p) for p in candidates):
            solved += 1
    return solved


def measure_role(manifest: Any, role: Any, workspace: Path) -> RoleCompleteness:
    learning = manifest.repo_path(workspace, role.learning_repo)
    on_disk = _module_dirs(learning)
    plan = {m.get("id"): m for m in _plan_modules(learning) if m.get("id")}
    solutions_root = (manifest.repo_path(workspace, role.solution_repo) / "modules"
                      if role.solution_repo and not role.is_single else None)
    modules: list[ModuleCompleteness] = []
    for module_id in sorted(set(plan) | set(on_disk)):
        if module_id not in on_disk:
            planned_ex = plan[module_id].get("exercises") or []
            modules.append(ModuleCompleteness(
                module_id, "not_started", exercises=len(planned_ex) if isinstance(planned_ex, list) else 0,
                solved=None if solutions_root is None else 0))
            continue
        depth = learning_module_depth(on_disk[module_id])
        numbers = _exercise_numbers(on_disk[module_id])
        solved = None if solutions_root is None else _solved(solutions_root / module_id, numbers)
        modules.append(ModuleCompleteness(
            module_id, depth.status, depth.words, depth.chapters, len(numbers), solved, depth.has_quiz))
    return RoleCompleteness(role.id, role.title, role.level, role.learning_repo,
                            None if role.is_single else role.solution_repo, tuple(modules))


def measure_org(manifest: Any, workspace: Path) -> list[RoleCompleteness]:
    return [measure_role(manifest, r, workspace) for r in sorted(manifest.roles, key=lambda r: (r.level, r.title))]


# --- rendering ---------------------------------------------------------------------


def _bar(pct: float | None) -> str:
    if pct is None:
        return "n/a"
    filled = int(round(pct / 10))
    return f"`{'█' * filled}{'░' * (10 - filled)}` {pct:.0f}%"


def _pct(pct: float | None) -> str:
    return "n/a" if pct is None else f"{pct:.0f}%"


def _generated(today: str) -> str:
    return f"_Generated {today} by `aicg org completeness`._"


def _method() -> list[str]:
    return [
        "## How this is calculated\n",
        f"- **Module complete**: at least {MIN_MODULE_WORDS:,} words of written content. Below that it's a "
        "**skeleton** (files exist, the module was never written).",
        "- **Not started**: in the curriculum plan, not on disk yet.",
        "- **Exercise solved**: its solution has real content, not a scaffold stub.",
        "- **Learning %** = complete modules / all modules. **Solutions %** = solved exercises / all "
        "exercises. **Overall** = the mean of the two (learning only where there's no solutions repo).",
        "- Skeletons and stubs are queued for the content pipeline automatically, entry-level roles first.",
        "",
    ]


def _repo_url(org: str, repo: str) -> str:
    return f"https://github.com/{org.lower()}/{repo}"


def render_role_matrix(manifest: Any, rc: RoleCompleteness, today: str | None = None) -> str:
    today = today or date.today().isoformat()
    org = manifest.org
    single = rc.solution_repo is None
    lines = [
        f"# Completeness Matrix: {rc.title}\n",
        f"{_org_display(org)} Curriculum · Level {rc.level} · "
        f"[learning]({_repo_url(org, rc.learning_repo)})"
        + ("" if single else f" · [solutions]({_repo_url(org, rc.solution_repo)})") + "\n",
        _generated(today) + "\n",
        f"**Overall: {_pct(rc.overall_pct)}**\n",
        "| | Progress | Count |",
        "|---|---|---|",
        f"| Learning | {_bar(rc.learning_pct)} | {rc.modules_complete}/{rc.modules_total} modules complete |",
    ]
    if not single:
        lines.append(f"| Solutions | {_bar(rc.solutions_pct)} | "
                     f"{rc.exercises_solved}/{rc.exercises_total} exercises solved |")
    lines += ["", "## Modules\n"]
    if not rc.modules:
        lines += ["No modules yet. The pipeline seeds a curriculum plan for this role and writes "
                  "the modules on its next fill run.", ""]
    else:
        head = "| Module | Status | Words | Chapters | Exercises | Quiz |" + ("" if single else " Solutions |")
        lines += [head, "|" + "---|" * (6 if single else 7)]
        for m in rc.modules:
            quiz = "✓" if m.has_quiz else "—"
            words = f"{m.words:,}" if m.status != "not_started" else "—"
            chapters = str(m.chapters) if m.status != "not_started" else "—"
            row = (f"| `{m.module_id}` | {_STATUS_LABEL[m.status]} | {words} | {chapters} | "
                   f"{m.exercises} | {quiz if m.status != 'not_started' else '—'} |")
            if not single:
                row += f" {m.solved or 0}/{m.exercises} |"
            lines.append(row)
        lines.append("")
    return "\n".join(lines + _method()).rstrip() + "\n"


def _role_link(org: str, rc: RoleCompleteness) -> str:
    return f"[{rc.title}]({_repo_url(org, rc.learning_repo)}/blob/main/{MATRIX_FILE})"


def _totals(roles: Iterable[RoleCompleteness]) -> tuple[int, int, int, int, float]:
    roles = list(roles)
    mc = sum(r.modules_complete for r in roles)
    mt = sum(r.modules_total for r in roles)
    es = sum(r.exercises_solved for r in roles if r.solution_repo)
    et = sum(r.exercises_total for r in roles if r.solution_repo)
    overall = sum(r.overall_pct for r in roles) / len(roles) if roles else 0.0
    return mc, mt, es, et, overall


def render_org_matrix(manifest: Any, roles: list[RoleCompleteness], today: str | None = None) -> str:
    today = today or date.today().isoformat()
    org = manifest.org
    mc, mt, es, et, overall = _totals(roles)
    lines = [
        f"# Completeness Matrix: {_org_display(org)} Curriculum\n",
        _generated(today) + "\n",
        f"**Overall: {overall:.0f}%** across {len(roles)} role tracks · {mc}/{mt} modules complete"
        + (f" · {es}/{et} exercises solved" if et else "") + "\n",
        "| Role | Level | Modules | Learning | Solutions | Overall | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in roles:
        lines.append(f"| {_role_link(org, r)} | L{r.level} | {r.modules_complete}/{r.modules_total} | "
                     f"{_pct(r.learning_pct)} | {_pct(r.solutions_pct)} | {_bar(r.overall_pct)} | {r.status_label} |")
    lines.append(f"| **Total** | | **{mc}/{mt}** | | | **{overall:.0f}%** | |")
    lines.append("")
    return "\n".join(lines + _method()).rstrip() + "\n"


def render_family_matrix(orgs: list[tuple[Any, list[RoleCompleteness]]], today: str | None = None) -> str:
    today = today or date.today().isoformat()
    all_roles = [r for _, roles in orgs for r in roles]
    mc, mt, es, et, overall = _totals(all_roles)
    lines = [
        "# Completeness Matrix: AI Career Curriculum\n",
        _generated(today) + "\n",
        f"**Overall: {overall:.0f}%** across {len(orgs)} curricula and {len(all_roles)} role tracks · "
        f"{mc}/{mt} modules complete · {es}/{et} exercises solved\n",
        "| Curriculum | Roles | Modules | Exercises solved | Overall | Details |",
        "|---|---|---|---|---|---|",
    ]
    for manifest, roles in orgs:
        omc, omt, oes, oet, oo = _totals(roles)
        link = f"https://github.com/{manifest.org.lower()}/.github/blob/main/{MATRIX_FILE}"
        ex = f"{oes}/{oet}" if oet else "n/a"
        lines.append(f"| **{_org_display(manifest.org)}** | {len(roles)} | {omc}/{omt} | {ex} | "
                     f"{_bar(oo)} | [matrix]({link}) |")
    lines.append("")
    return "\n".join(lines + _method()).rstrip() + "\n"


# --- writing -----------------------------------------------------------------------


def same_except_date(a: str, b: str) -> bool:
    return _GENERATED_RE.sub("", a) == _GENERATED_RE.sub("", b)


def _write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and same_except_date(path.read_text(encoding="utf-8"), text):
        return False
    path.write_text(text, encoding="utf-8")
    return True


def write_matrices(manifest: Any, workspace: Path, today: str | None = None) -> list[Path]:
    """Write role matrices into each existing role repo and the org matrix into ``.github``.

    Repos that aren't checked out are skipped. Returns the files whose content
    changed (date-only changes don't count, to avoid churn).
    """
    roles = measure_org(manifest, workspace)
    by_id = {r.id: r for r in manifest.roles}
    written: list[Path] = []
    for rc in roles:
        text = render_role_matrix(manifest, rc, today)
        role = by_id[rc.role_id]
        for repo in role.repos:
            target = manifest.repo_path(workspace, repo)
            if target.is_dir() and _write_if_changed(target / MATRIX_FILE, text):
                written.append(target / MATRIX_FILE)
    dotgithub = manifest.repo_path(workspace, ".github")
    if dotgithub.is_dir() and _write_if_changed(dotgithub / MATRIX_FILE, render_org_matrix(manifest, roles, today)):
        written.append(dotgithub / MATRIX_FILE)
    return written


def write_family_matrix(manifests: list[Any], workspace: Path, out: Path, today: str | None = None) -> bool:
    orgs = [(m, measure_org(m, workspace)) for m in manifests]
    out.parent.mkdir(parents=True, exist_ok=True)
    return _write_if_changed(out, render_family_matrix(orgs, today))
