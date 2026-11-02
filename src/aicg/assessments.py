"""Backfill quizzes and labs for modules that already have lecture content.

Bootstrap scaffolds every module with placeholder ``quizzes/README.md`` and
``labs/README.md`` files, but nothing authored them afterwards: the research
cycle proposes new modules, ``generate-learning`` writes chapters and exercise
prompts, and the solution filler writes solutions. This module closes that gap.

For each module that is authored (has chapters) but whose ``quizzes/`` or
``labs/`` directory still holds only the placeholder README, it asks the
configured content agent to write the assessments from the module's own
chapters. The agent may edit only those two directories. Any change outside
them makes the whole attempt for that module get reverted and reported as
rejected, so a confused or over-eager agent can't rewrite chapters.

Like ``learning_content``, committing and pushing is the caller's job.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_cli import AgentLimitReached, run_agent_command
from .org_config import OrgManifest, RoleConfig
from .org_runner import content_generation_command
from .state import ensure_state_dir, utc_now, write_json

ASSESSMENT_REPORT = "assessment-backfill-report.json"
_RUNNER_ROOT = Path(__file__).resolve().parents[2]
_CHAPTER_RE = re.compile(r"^\d\d-.+\.md$")
# Module ids come from a repo file and flow into paths and the agent command,
# so only plain slugs are accepted.
_MODULE_ID_RE = re.compile(r"^mod-[a-z0-9][a-z0-9-]*$")
_STATE_PREFIX = ".aicg/"
# Paths git doesn't report but that change what git itself does (hooks run on
# the job's commit). Snapshotted and restored around every agent run.
_GIT_PROTECTED = (".git/config", ".git/hooks", ".git/HEAD", ".git/packed-refs", ".git/info")


class AssessmentError(RuntimeError):
    """Raised when assessments cannot be backfilled."""


@dataclass(frozen=True)
class AssessmentGap:
    module_id: str
    needs_quiz: bool
    needs_labs: bool


# --- detection -----------------------------------------------------------------


def chapter_sources(module_dir: Path) -> list[Path]:
    """Lecture files for a module, across the layouts used in the fleet.

    Three layouts exist: numbered chapters (``01-intro.md``), a single
    ``lecture-notes.md``, or a ``lecture-notes/`` directory.
    """
    if not module_dir.is_dir():
        return []
    sources = sorted(p for p in module_dir.glob("*.md") if _CHAPTER_RE.match(p.name))
    single = module_dir / "lecture-notes.md"
    if single.is_file():
        sources.append(single)
    notes_dir = module_dir / "lecture-notes"
    if notes_dir.is_dir():
        sources.extend(sorted(notes_dir.rglob("*.md")))
    return sources


def module_is_authored(module_dir: Path) -> bool:
    """True when the module has lecture content (not just a scaffold)."""
    return bool(chapter_sources(module_dir))


# Phrases the bootstrap scaffolder writes into placeholder READMEs.
PLACEHOLDER_MARKERS = (
    "authored under the autonomous fill-in loop",
    "will be authored on subsequent cycles",
)
# A README shorter than this many non-blank lines can't be a real quiz or lab.
_MIN_CONTENT_LINES = 8


def _readme_is_placeholder(readme: Path) -> bool:
    text = readme.read_text(encoding="utf-8", errors="replace")
    if any(marker in text.lower() for marker in PLACEHOLDER_MARKERS):
        return True
    return sum(1 for line in text.splitlines() if line.strip()) < _MIN_CONTENT_LINES


def _only_placeholder(directory: Path) -> bool:
    """True when ``directory`` exists and holds nothing but a placeholder README.

    Some modules put the real quiz inside ``quizzes/README.md`` itself, so a
    lone README only counts as a placeholder if it looks like one.
    """
    if not directory.is_dir():
        return False
    markdown = list(directory.rglob("*.md"))
    if any(p.name.lower() != "readme.md" for p in markdown):
        return False
    return all(_readme_is_placeholder(p) for p in markdown)


def _has_root_quiz(module_dir: Path) -> bool:
    return any(module_dir.glob("quiz*.md"))


def find_assessment_gaps(learning_path: Path, plan: dict[str, Any]) -> list[AssessmentGap]:
    """Authored modules in plan order whose quizzes or labs are placeholders.

    Modules without ``quizzes/`` or ``labs/`` directories use an older layout
    and are left alone: this never invents structure a repo doesn't use.
    """
    gaps: list[AssessmentGap] = []
    for module in plan.get("modules", []) or []:
        if not _MODULE_ID_RE.match(str(module.get("id", ""))):
            continue
        module_dir = learning_path / "lessons" / module["id"]
        if not module_is_authored(module_dir):
            continue
        needs_quiz = _only_placeholder(module_dir / "quizzes") and not _has_root_quiz(module_dir)
        needs_labs = _only_placeholder(module_dir / "labs")
        if needs_quiz or needs_labs:
            gaps.append(AssessmentGap(module["id"], needs_quiz, needs_labs))
    return gaps


# --- prompt ----------------------------------------------------------------------


def _targets(mod_id: str, gap: AssessmentGap) -> str:
    lines = []
    if gap.needs_quiz:
        lines.append(
            f"- **Quiz** in `lessons/{mod_id}/quizzes/`: one file "
            f"`quiz-01-<slug>.md` with 8-12 multiple-choice questions covering every "
            f"chapter. Each question tests understanding, not recall of wording. Put an "
            f"answer key at the bottom with a one-to-two sentence explanation per answer "
            f"that points to the chapter it comes from. Update `quizzes/README.md` to "
            f"link the quiz."
        )
    if gap.needs_labs:
        lines.append(
            f"- **Labs** in `lessons/{mod_id}/labs/`: one to three files "
            f"`lab-0N-<slug>.md`, each a guided, step-by-step hands-on session "
            f"(duration, prerequisites, objective, numbered steps with commands or "
            f"code, expected output, and a short checkpoint). Labs are guided practice; "
            f"they must not duplicate the module's exercises. Update `labs/README.md` "
            f"to index them."
        )
    return "\n".join(lines)


def build_assessment_prompt(
    role_title: str,
    level: int | None,
    module: dict[str, Any],
    gap: AssessmentGap,
    source_files: list[str],
) -> str:
    """Build the content-agent prompt for one module's assessments."""
    mod_id = module["id"]
    title = module.get("title", mod_id)
    objectives = (
        "\n".join(f"- {o}" for o in module.get("objectives", []) or []) or "- (none listed)"
    )
    sources = "\n".join(f"- `{s}`" for s in source_files)
    allowed = " and ".join(
        f"`lessons/{mod_id}/{d}/`"
        for d, needed in (("quizzes", gap.needs_quiz), ("labs", gap.needs_labs))
        if needed
    )
    return (
        f"# AICG Assessment Packet: {mod_id}\n\n"
        f"## Goal\n\n"
        f"Write the assessments for module `{mod_id}` ({title}) of the **{role_title}** "
        f"track (level {level}). The lecture chapters are already written. Base every "
        f"question and lab step on what those chapters actually teach.\n\n"
        f"## Learning objectives\n\n{objectives}\n\n"
        f"## Read first (source material)\n\n{sources}\n\n"
        f"Also read the module `README.md` and its `exercises/` so labs don't repeat them.\n\n"
        f"## What to write\n\n{_targets(mod_id, gap)}\n\n"
        f"## Scope & guardrails\n\n"
        f"- Edit ONLY files under {allowed}. Any change elsewhere (chapters, exercises, "
        f"README, other modules) causes this whole attempt to be discarded.\n"
        f"- Match the format of existing quizzes and labs elsewhere in this org.\n"
        f"- Do NOT invent facts, metrics, or tool behavior the chapters don't support.\n"
        f"- If a claim cannot be verified, write `<!-- needs-research: ... -->` instead of "
        f"guessing.\n"
    )


# --- change control ----------------------------------------------------------------


# Runner scratch the harness itself writes during a run.
_SCRATCH_PREFIXES = (".aicg/prompts/", ".aicg/generated/", f".aicg/{ASSESSMENT_REPORT}")


def disallowed_changes(changed: list[str], module_id: str) -> list[str]:
    """Paths outside the module's ``quizzes/`` and ``labs/`` (runner scratch excluded).

    Tracked files under ``.aicg/`` (such as ``curriculum-plan.json``) are NOT
    exempt: only the scratch the harness writes itself.
    """
    allowed = (f"lessons/{module_id}/quizzes/", f"lessons/{module_id}/labs/")
    return [
        path
        for path in changed
        if not path.startswith(_SCRATCH_PREFIXES) and not path.startswith(allowed)
    ]


def _user_changes(repo: Path, paths: list[str]) -> list[str]:
    """Changed paths, excluding untracked runner state under ``.aicg/``."""
    return [p for p in paths if not (p.startswith(_STATE_PREFIX) and not _in_head(repo, p))]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssessmentError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


def changed_paths(repo: Path) -> list[str]:
    """Modified, deleted, and untracked (non-ignored) paths, one per file."""
    out = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = [e for e in out.split("\0") if e]
    paths: list[str] = []
    skip_next = False
    for entry in entries:
        if skip_next:  # rename/copy source path follows the destination
            skip_next = False
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        skip_next = "R" in status or "C" in status
    return paths


Snapshot = dict[str, bytes | None]


def _read(repo: Path, path: str) -> bytes | None:
    target = repo / path
    return target.read_bytes() if target.is_file() else None


def _snapshot(repo: Path) -> Snapshot:
    """Current bytes of every already-changed path (None = deleted/missing)."""
    return {p: _read(repo, p) for p in _user_changes(repo, changed_paths(repo))}


def touched_since(repo: Path, before: Snapshot) -> list[str]:
    """Paths whose content differs from ``before`` (new changes, or re-edits of
    paths that were already dirty, e.g. an earlier module's fresh quiz)."""
    candidates = set(_user_changes(repo, changed_paths(repo))) | set(before)
    touched = []
    for path in sorted(candidates):
        if path in before:
            if _read(repo, path) != before[path]:
                touched.append(path)
        else:
            touched.append(path)
    return touched


def _in_head(repo: Path, path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{path}"], cwd=repo, capture_output=True, check=False
    )
    return result.returncode == 0


def _protected_snapshot(repo: Path) -> Snapshot:
    """Bytes of git-invisible files that alter git's own behavior."""
    snap: Snapshot = {}
    for rel in _GIT_PROTECTED:
        target = repo / rel
        if target.is_file():
            snap[rel] = target.read_bytes()
        elif target.is_dir():
            for f in sorted(p for p in target.rglob("*") if p.is_file()):
                snap[str(f.relative_to(repo))] = f.read_bytes()
    return snap


def protected_touched(repo: Path, before: Snapshot) -> list[str]:
    after = _protected_snapshot(repo)
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def _restore_protected(repo: Path, before: Snapshot) -> None:
    for rel in protected_touched(repo, before):
        target = repo / rel
        if rel in before:
            target.write_bytes(before[rel])  # type: ignore[arg-type]
        else:
            target.unlink(missing_ok=True)


def _head_state(repo: Path) -> tuple[str, str | None]:
    """(HEAD commit sha, symbolic ref such as refs/heads/main or None if detached)."""
    sha = _git(repo, "rev-parse", "HEAD").strip()
    ref = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=repo, capture_output=True, text=True, check=False
    ).stdout.strip()
    return sha, ref or None


def _restore_head(repo: Path, sha: str, ref: str | None) -> bool:
    """Put the branch back on ``sha`` if the agent moved it. True if it had moved.

    The agent can't run ``git commit``, but it can write ref files directly;
    without this a smuggled commit would ride along on the job's push.
    """
    if _head_state(repo) == (sha, ref):
        return False
    if ref:
        _git(repo, "update-ref", ref, sha)
        _git(repo, "symbolic-ref", "HEAD", ref)
    else:
        _git(repo, "update-ref", "--no-deref", "HEAD", sha)
    return True


def _revert_fail_closed(
    repo: Path, touched: list[str], before: Snapshot, git_before: Snapshot
) -> None:
    """Undo an attempt. If precise restore fails, fall back to a hard reset
    (the run requires a clean repo up front) and raise so nothing is committed."""
    try:
        _restore_protected(repo, git_before)
        revert_paths(repo, touched, before)
    except (OSError, AssessmentError) as exc:
        _git(repo, "reset", "-q", "--hard", "HEAD")
        _git(repo, "clean", "-fdq")
        raise AssessmentError(f"precise revert failed ({exc}); repo hard-reset to HEAD") from exc


def revert_paths(repo: Path, paths: list[str], before: Snapshot) -> None:
    """Restore each path to its state in ``before`` (or HEAD if it was clean)."""
    for path in paths:
        target = repo / path
        if path in before:
            original = before[path]
            if original is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(original)
        elif _in_head(repo, path):
            _git(repo, "checkout", "-q", "HEAD", "--", path)
        else:
            target.unlink(missing_ok=True)


# --- orchestration -------------------------------------------------------------------


def _load_role(
    manifest: OrgManifest, workspace: Path, role_id: str
) -> tuple[RoleConfig, Path, dict]:
    role = next((item for item in manifest.roles if item.id == role_id), None)
    if role is None:
        raise AssessmentError(f"Role {role_id!r} not in manifest.")
    learning_path = workspace.resolve() / role.learning_repo
    if not (learning_path / ".git").exists():
        raise AssessmentError(f"Learning repo is not a git checkout: {learning_path}")
    plan_path = learning_path / ".aicg" / "curriculum-plan.json"
    if not plan_path.exists():
        raise AssessmentError(f"Curriculum plan not found at {plan_path}.")
    return role, learning_path, json.loads(plan_path.read_text(encoding="utf-8"))


def _attempt_module(
    command: str, learning_path: Path, role: RoleConfig, plan: dict, gap: AssessmentGap
) -> tuple[str, list[str]]:
    """Run the agent for one module. Returns (outcome, rejected_paths)."""
    module = next(m for m in plan["modules"] if m["id"] == gap.module_id)
    module_dir = learning_path / "lessons" / gap.module_id
    sources = [str(p.relative_to(learning_path)) for p in chapter_sources(module_dir)]
    prompt = build_assessment_prompt(
        plan.get("title", role.title), plan.get("level", role.level), module, gap, sources
    )
    state_root = ensure_state_dir(learning_path)
    work_id = f"assessments-{gap.module_id}"
    prompt_path = state_root / "prompts" / f"{work_id}.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    output_dir = state_root / "generated" / work_id
    output_dir.mkdir(parents=True, exist_ok=True)
    formatted = command.format(
        prompt=str(prompt_path),
        output_dir=str(output_dir),
        repo=str(learning_path),
        work_id=work_id,
        runner=str(_RUNNER_ROOT),
    )
    before = _snapshot(learning_path)
    git_before = _protected_snapshot(learning_path)
    head_sha, head_ref = _head_state(learning_path)
    result = run_agent_command(formatted, cwd=learning_path)
    # Restore git's own state first, so file diffs are measured against the
    # original HEAD rather than one the agent may have moved.
    violations = protected_touched(learning_path, git_before)
    _restore_protected(learning_path, git_before)
    if _restore_head(learning_path, head_sha, head_ref):
        violations.append(f"HEAD moved off {head_ref or head_sha}")
    touched = touched_since(learning_path, before)
    if result.limit_reached:
        _revert_fail_closed(learning_path, touched, before, git_before)
        raise AgentLimitReached(result)
    rejected = disallowed_changes(touched, gap.module_id) + violations
    if rejected or result.returncode != 0:
        _revert_fail_closed(learning_path, touched, before, git_before)
        return ("rejected" if rejected else "failed"), rejected
    return ("filled" if touched else "unchanged"), []


def _empty_report(role: RoleConfig) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "backfill_role_assessments",
        "role_id": role.id,
        "learning_repo": role.learning_repo,
        "modules_filled": [],
        "modules_unchanged": [],
        "modules_failed": [],
        "modules_rejected": [],
        "rejections": {},
        "deferred_module": None,
        "gaps_remaining": 0,
    }


def backfill_role_assessments(
    manifest: OrgManifest,
    workspace: Path,
    role_id: str,
    max_modules: int = 1,
    command_override: str | None = None,
) -> dict[str, Any]:
    """Author quizzes/labs for up to ``max_modules`` gaps in one role."""
    role, learning_path, plan = _load_role(manifest, workspace, role_id)
    command = command_override or content_generation_command(manifest)
    if not command:
        raise AssessmentError("No content-generation command configured in the org manifest.")
    if _user_changes(learning_path, changed_paths(learning_path)):
        raise AssessmentError(
            f"{learning_path} has uncommitted changes; refusing to run (they would be "
            f"indistinguishable from the agent's edits)."
        )
    report = _empty_report(role)
    report["head_before"] = _head_state(learning_path)[0]
    for gap in find_assessment_gaps(learning_path, plan)[:max_modules]:
        try:
            outcome, rejected = _attempt_module(command, learning_path, role, plan, gap)
        except AgentLimitReached:
            report["deferred_module"] = gap.module_id
            break
        report[f"modules_{outcome}"].append(gap.module_id)
        if rejected:
            report["rejections"][gap.module_id] = rejected
    report["gaps_remaining"] = len(find_assessment_gaps(learning_path, plan))
    write_json(ensure_state_dir(learning_path) / ASSESSMENT_REPORT, report)
    return report


def roles_with_gaps(manifest: OrgManifest, workspace: Path) -> list[str]:
    """Role ids (lowest level first) whose learning repo has assessment gaps."""
    found: list[tuple[int, str]] = []
    for role in manifest.roles:
        learning_path = workspace.resolve() / role.learning_repo
        plan_path = learning_path / ".aicg" / "curriculum-plan.json"
        if not plan_path.exists():
            continue
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if find_assessment_gaps(learning_path, plan):
            found.append((role.level, role.id))
    return [role_id for _, role_id in sorted(found)]
