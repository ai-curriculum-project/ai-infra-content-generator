"""Resolve ``<!-- needs-research -->`` markers left in authored content.

Content agents are told to mark any claim they can't verify with a
``needs-research`` HTML comment instead of guessing. Nothing ever came back to
resolve them: by 2026-11 there were ~4,900 across 40 repos, each an unsourced
claim sitting in published material.

This module works through them one module at a time. The research agent (which
has web search) gets a batch of markers from one module and, for each, either
cites an authoritative source and removes the marker, rewrites the claim so it
no longer asserts what can't be verified, or leaves it if neither is possible.

Like ``assessments``, the agent works on an isolated copy of the checkout (no
``.git``). Its changes are copied back only if they pass ``judge_attempt``:
edits confined to the files holding the batch's markers, no new or deleted
files, no file gaining markers, at least one marker resolved, and a diff no
larger than a few lines per resolved marker, so a chapter can't be rewritten
wholesale under cover of "fixing a citation". Committing is the caller's job.
"""

from __future__ import annotations

import difflib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_cli import AgentLimitReached, run_agent_command
from .assessments import (
    _tree_files,
    _user_changes,
    changed_paths,
    checkout_fingerprint,
    head_sha,
    make_isolated_copy,
)
from .org_config import OrgManifest
from .org_runner import content_generation_command
from .state import ensure_state_dir, utc_now, write_json

MARKER_REPORT = "marker-resolution-report.json"
MARKER_RE = re.compile(r"<!--\s*needs-research\b(.*?)-->", re.IGNORECASE | re.DOTALL)
_MODULE_DIR_RE = re.compile(r"^(lessons|modules)/mod-[a-z0-9][a-z0-9-]*$")
_RUNNER_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BATCH = 25
DEFAULT_LINES_PER_MARKER = 12
_DIFF_SLACK_LINES = 10


class MarkerError(RuntimeError):
    """Raised when markers cannot be resolved for a repo."""


@dataclass(frozen=True)
class Marker:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class MarkerUnit:
    module_dir: str
    markers: tuple[Marker, ...]

    @property
    def count(self) -> int:
        return len(self.markers)


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    resolved: int
    changed: list[str]
    reason: str = ""


# --- detection -------------------------------------------------------------------


def count_markers(text: str) -> int:
    return len(MARKER_RE.findall(text))


def _markers_in(path: Path, rel: str) -> list[Marker]:
    text = path.read_text(encoding="utf-8", errors="replace")
    found = []
    for match in MARKER_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        found.append(Marker(rel, line, " ".join(match.group(1).split())[:300]))
    return found


def find_marker_units(repo_path: Path) -> list[MarkerUnit]:
    """Module directories with markers, most markers first."""
    units: list[MarkerUnit] = []
    for top in ("lessons", "modules"):
        root = repo_path / top
        if not root.is_dir():
            continue
        for module in sorted(p for p in root.iterdir() if p.is_dir()):
            rel_dir = module.relative_to(repo_path).as_posix()
            if not _MODULE_DIR_RE.match(rel_dir):
                continue
            markers = [
                m
                for f in sorted(module.rglob("*.md"))
                if f.is_file() and not f.is_symlink()
                for m in _markers_in(f, f.relative_to(repo_path).as_posix())
            ]
            if markers:
                units.append(MarkerUnit(rel_dir, tuple(markers)))
    return sorted(units, key=lambda u: (-u.count, u.module_dir))


# --- prompt ----------------------------------------------------------------------


def build_marker_prompt(
    repo_name: str, unit: MarkerUnit, batch_size: int = DEFAULT_BATCH
) -> tuple[str, list[Marker]]:
    """Prompt for one batch of a unit's markers. Returns (prompt, batch)."""
    batch = list(unit.markers[:batch_size])
    files = sorted({m.path for m in batch})
    listing = "\n".join(f"- `{m.path}:{m.line}` — {m.text}" for m in batch)
    file_list = "\n".join(f"- `{f}`" for f in files)
    return (
        f"# AICG Research Packet: resolve needs-research markers in `{unit.module_dir}`\n\n"
        f"Repository: `{repo_name}`. The content below was written by an earlier agent that "
        f"marked each claim it could not verify with a `<!-- needs-research: ... -->` comment. "
        f"Resolve these {len(batch)} markers (of {unit.count} in this module):\n\n{listing}\n\n"
        "## For each marker, do exactly one of\n\n"
        "1. **Verify and cite.** Use web search (WebSearch / WebFetch) to find an authoritative "
        "source: the statute, standard, regulator, official documentation, or primary research. "
        "If it supports the claim, add a concise inline citation or link and delete the marker. "
        "If it contradicts the claim, correct the claim to match the source, cite it, and delete "
        "the marker.\n"
        "2. **Soften.** If no authoritative source exists, rewrite the sentence so it no longer "
        "asserts the unverifiable specific (drop the number, name, or date; state what is "
        "generally true instead), and delete the marker.\n"
        "3. **Leave it.** If the claim is essential and you can neither verify nor safely soften "
        "it, leave the marker exactly as it is.\n\n"
        "## Scope & guardrails\n\n"
        f"- Edit ONLY these files:\n{file_list}\n"
        "- Change only the sentences carrying these markers (and a citation line if needed). "
        "Do not reflow, restyle, or rewrite surrounding text. Large diffs are rejected.\n"
        "- Do not create, delete, or rename files. Do not add new `needs-research` markers.\n"
        "- Never invent a source. A citation must be something you actually opened.\n"
        "- Prefer primary and official sources over blogs and vendor marketing.\n"
    ), batch


# --- judging -----------------------------------------------------------------------


def _changed_lines(before: str, after: str) -> int:
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
    return sum(1 for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---")))


def judge_attempt(
    before: dict[str, str],
    after: dict[str, str],
    allowed: set[str],
    lines_per_marker: int = DEFAULT_LINES_PER_MARKER,
) -> Verdict:
    """Decide whether an attempt's file changes may be kept. Pure."""
    keys = sorted(set(before) | set(after))
    changed = [k for k in keys if before.get(k) != after.get(k)]
    if not changed:
        return Verdict(False, 0, [], "no changes")
    created = [k for k in changed if k not in before]
    deleted = [k for k in changed if k not in after]
    outside = [k for k in changed if k not in allowed]
    if created or deleted or outside:
        bad = created + deleted + [k for k in outside if k not in created + deleted]
        return Verdict(False, 0, changed, "edits outside the marker files: " + ", ".join(bad[:10]))
    gained = [k for k in changed if count_markers(after[k]) > count_markers(before[k])]
    if gained:
        return Verdict(False, 0, changed, "markers added in: " + ", ".join(gained))
    resolved = sum(count_markers(before[k]) - count_markers(after[k]) for k in changed)
    if resolved <= 0:
        return Verdict(False, 0, changed, "no marker resolved")
    lines = sum(_changed_lines(before[k], after[k]) for k in changed)
    budget = lines_per_marker * resolved + _DIFF_SLACK_LINES
    if lines > budget:
        return Verdict(
            False,
            resolved,
            changed,
            f"{lines} lines changed for {resolved} markers (budget {budget})",
        )
    return Verdict(True, resolved, changed)


# --- orchestration -------------------------------------------------------------------


def _manifest_repos(manifest: OrgManifest) -> list[str]:
    names: list[str] = []
    for role in manifest.roles:
        names.append(role.learning_repo)
        if role.solution_repo:
            names.append(role.solution_repo)
    return list(dict.fromkeys(names))


def _agent_command(manifest: OrgManifest, override: str | None) -> str:
    if override:
        return override
    from .research import ResearchAgentConfig  # web-enabled agent

    command = ResearchAgentConfig.from_manifest(manifest).agent_command
    command = command or content_generation_command(manifest)
    if not command:
        raise MarkerError("No research or content agent command configured in the manifest.")
    return command


def _text_files(root: Path) -> dict[str, str]:
    """Markdown files under ``root`` (no .git, no runner state), as text."""
    return {
        rel: data.decode("utf-8", errors="replace")
        for rel, data in _tree_files(root, skip_git=True).items()
        if rel.endswith(".md") and not rel.startswith(".aicg/")
    }


def _safe_write(repo: Path, rel: str, content: str) -> None:
    root = repo.resolve()
    current = root
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            raise MarkerError(f"refusing to write through symlink: {current}")
    target = root / rel
    if not target.resolve().is_relative_to(root):
        raise MarkerError(f"refusing to write outside the repo: {rel}")
    target.write_text(content, encoding="utf-8")


def _run_unit(
    command: str,
    repo: Path,
    repo_name: str,
    unit: MarkerUnit,
    batch_size: int,
    lines_per_marker: int,
) -> Verdict:
    with tempfile.TemporaryDirectory(prefix="aicg-markers-") as tmp:
        work = Path(tmp) / "repo"
        make_isolated_copy(repo, work)
        prompt, batch = build_marker_prompt(repo_name, unit, batch_size)
        work_id = "markers-" + unit.module_dir.replace("/", "-")
        prompt_path = work / ".aicg" / "prompts" / f"{work_id}.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        output_dir = work / ".aicg" / "generated" / work_id
        output_dir.mkdir(parents=True, exist_ok=True)
        formatted = command.format(
            prompt=str(prompt_path),
            output_dir=str(output_dir),
            repo=str(work),
            work_id=work_id,
            runner=str(_RUNNER_ROOT),
            artifact=unit.module_dir,
        )
        fingerprint = checkout_fingerprint(repo)
        result = run_agent_command(formatted, cwd=work)
        if checkout_fingerprint(repo) != fingerprint:
            raise MarkerError(f"real checkout {repo} changed during the agent run; aborting")
        if result.limit_reached:
            raise AgentLimitReached(result)
        verdict = judge_attempt(
            _text_files(repo), _text_files(work), {m.path for m in batch}, lines_per_marker
        )
        if verdict.accepted and result.returncode == 0:
            after = _text_files(work)
            for rel in verdict.changed:
                _safe_write(repo, rel, after[rel])
        elif result.returncode != 0:
            return Verdict(False, 0, [], f"agent exited {result.returncode}")
        return verdict


def _empty_report(repo_name: str, head: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "resolve_markers",
        "repo": repo_name,
        "head_before": head,
        "units_resolved": [],
        "units_rejected": [],
        "units_failed": [],
        "units_unchanged": [],
        "rejections": {},
        "markers_resolved": 0,
        "files_changed": [],
        "deferred_unit": None,
        "markers_remaining": 0,
    }


def _record(report: dict[str, Any], unit: MarkerUnit, verdict: Verdict) -> None:
    if verdict.accepted:
        report["units_resolved"].append(unit.module_dir)
        report["markers_resolved"] += verdict.resolved
        report["files_changed"] += verdict.changed
    elif verdict.reason == "no changes":
        report["units_unchanged"].append(unit.module_dir)
    elif verdict.reason.startswith("agent exited"):
        report["units_failed"].append(unit.module_dir)
    else:
        report["units_rejected"].append(unit.module_dir)
        report["rejections"][unit.module_dir] = verdict.reason


def resolve_repo_markers(
    manifest: OrgManifest,
    workspace: Path,
    repo_name: str,
    max_units: int = 1,
    batch_size: int = DEFAULT_BATCH,
    command_override: str | None = None,
    lines_per_marker: int = DEFAULT_LINES_PER_MARKER,
) -> dict[str, Any]:
    """Resolve one batch of markers in each of up to ``max_units`` modules."""
    if repo_name not in _manifest_repos(manifest):
        raise MarkerError(f"Repo {repo_name!r} not in manifest.")
    repo = workspace.resolve() / repo_name
    if not (repo / ".git").exists():
        raise MarkerError(f"Not a git checkout: {repo}")
    command = _agent_command(manifest, command_override)
    if _user_changes(repo, changed_paths(repo)):
        raise MarkerError(f"{repo} has uncommitted changes; refusing to run.")
    report = _empty_report(repo_name, head_sha(repo))
    for unit in find_marker_units(repo)[:max_units]:
        try:
            verdict = _run_unit(command, repo, repo_name, unit, batch_size, lines_per_marker)
        except AgentLimitReached:
            report["deferred_unit"] = unit.module_dir
            break
        _record(report, unit, verdict)
    report["markers_remaining"] = sum(u.count for u in find_marker_units(repo))
    write_json(ensure_state_dir(repo) / MARKER_REPORT, report)
    return report


def repos_with_markers(manifest: OrgManifest, workspace: Path) -> list[tuple[str, int]]:
    """(repo, marker count) for role repos on disk with markers, most first."""
    found = []
    for name in _manifest_repos(manifest):
        repo = workspace.resolve() / name
        if repo.is_dir():
            total = sum(u.count for u in find_marker_units(repo))
            if total:
                found.append((name, total))
    return sorted(found, key=lambda x: (-x[1], x[0]))
