"""Author learning content (lecture chapters + exercise prompts) for a role
directly from its ``curriculum-plan.json``.

The research cycle (``aicg org research``) authors *new* modules but is
evidence-gated — it only proposes modules backed by >=3 recent job
postings, so freshly bootstrapped roles with sparse-title markets stay
empty. The solution generator (``aicg generate``) only fills *solutions*
for learning exercises that already exist.

This module bridges the gap: it reads the seeded curriculum plan and asks
the configured content agent to write the lecture chapters and flesh out
the exercise prompts for each module, grounded in the plan's objectives —
no job-postings evidence required. Solutions are still produced by the
existing gap-filler afterward.

The agent edits the learning repo's working tree in place; committing and
PR-ing is the caller's responsibility (mirrors the other org content
flows).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agent_cli import AgentLimitReached, run_agent_command
from .assessments import module_is_authored
from .org_config import OrgManifest
from .org_runner import content_generation_command
from .state import ensure_state_dir, utc_now, write_json

LEARNING_CONTENT_REPORT = "learning-content-report.json"
_RUNNER_ROOT = Path(__file__).resolve().parents[2]


class LearningContentError(RuntimeError):
    """Raised when learning content cannot be generated."""


def build_module_content_prompt(
    role_id: str,
    role_title: str,
    level: int | None,
    module: dict[str, Any],
    sibling_module_ids: list[str],
) -> str:
    """Build the content-agent prompt for one module."""
    mod_id = module["id"]
    title = module.get("title", mod_id)
    objectives = module.get("objectives", []) or []
    exercises = module.get("exercises", []) or []

    objective_lines = "\n".join(f"- {obj}" for obj in objectives) or "- (none listed)"
    exercise_lines = (
        "\n".join(
            f"- `lessons/{mod_id}/exercises/exercise-"
            f"{ex.get('id', '').split('-')[-1] or '01'}-{ex.get('slug', 'exercise')}.md` "
            f"— {ex.get('slug', 'exercise').replace('-', ' ')}"
            for ex in exercises
        )
        or "- (none listed)"
    )
    siblings = ", ".join(m for m in sibling_module_ids if m != mod_id) or "(none)"

    return (
        f"# AICG Learning Content Packet: {mod_id}\n\n"
        f"## Goal\n\n"
        f"Author the **learning content** for module `{mod_id}` ({title}) of the "
        f"**{role_title}** track (role `{role_id}`, level {level}). Write the lecture "
        f"chapters and flesh out the exercise prompts, grounded in the learning "
        f"objectives below. This is curriculum authoring from the plan — you do NOT "
        f"need job-postings evidence.\n\n"
        f"## Learning objectives (the spec for this module)\n\n"
        f"{objective_lines}\n\n"
        f"## What to write (in `lessons/{mod_id}/`)\n\n"
        f"- **Lecture chapters** `01-<slug>.md`, `02-<slug>.md`, ... — one focused "
        f"chapter per major objective. Each chapter: a clear `#` H1 title, motivation, "
        f"core concepts, concrete examples/code, and a short summary. Replace the "
        f"placeholder `README.md` so it indexes the chapters.\n"
        f"- **Exercise prompts** — flesh out each placeholder file with a real, "
        f"hands-on prompt: problem statement, requirements, starter guidance, and "
        f"acceptance criteria. Do NOT write the solution (solutions live in the paired "
        f"`-solutions` repo and are generated separately):\n"
        f"{exercise_lines}\n"
        f"- **`resources.md`** — real, citable references for the topics covered.\n\n"
        f"## Scope & guardrails\n\n"
        f"- Edit ONLY files under `lessons/{mod_id}/`. Do not touch other modules "
        f"({siblings}) or the solutions repo.\n"
        f"- Match the layout and tone of existing tracks in this org.\n"
        f"- Use official standards and primary documentation first.\n"
        f"- Do NOT invent facts, metrics, incidents, benchmarks, or case studies.\n"
        f"- If a factual claim cannot be verified, write `<!-- needs-research: ... -->` "
        f"(this blocks auto-merge) rather than guessing.\n"
        f"- Keep each chapter focused; prefer several small chapters over one long file.\n"
    )


def generate_role_learning_content(
    manifest: OrgManifest,
    workspace: Path,
    role_id: str,
    module: str | None = None,
    command_override: str | None = None,
    state_dir: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Generate learning content for one role's modules from its plan.

    Modules that already have lecture content are skipped unless
    ``overwrite`` is set. The per-role ``generate-role`` timer re-runs this
    monthly over the whole plan; without the guard it would ask the agent to
    re-author hand-written or reviewed modules.
    """
    role = next((item for item in manifest.roles if item.id == role_id), None)
    if role is None:
        raise LearningContentError(
            f"Role {role_id!r} not in manifest. Run `aicg org bootstrap-role` first."
        )

    workspace = workspace.resolve()
    learning_path = workspace / role.learning_repo
    if not learning_path.exists():
        raise LearningContentError(f"Learning repo not on disk: {learning_path}")

    plan_path = learning_path / ".aicg" / "curriculum-plan.json"
    if not plan_path.exists():
        raise LearningContentError(
            f"Curriculum plan not found at {plan_path}. Author/seed it first."
        )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    modules = plan.get("modules", []) or []
    sibling_ids = [m["id"] for m in modules]
    if module is not None:
        modules = [m for m in modules if m["id"] == module]
        if not modules:
            raise LearningContentError(f"Module {module!r} not in plan for {role_id!r}.")

    command = command_override or content_generation_command(manifest)
    if not command:
        raise LearningContentError(
            "No content-generation command configured in the org manifest."
        )

    state_root = ensure_state_dir(learning_path)
    prompt_dir = state_root / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)

    generated: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    timed_out: list[str] = []
    deferred: str | None = None

    for mod in modules:
        if not overwrite and module_is_authored(learning_path / "lessons" / mod["id"]):
            skipped.append(mod["id"])
            continue
        prompt = build_module_content_prompt(
            role_id=role_id,
            role_title=plan.get("title", role.title),
            level=plan.get("level", role.level),
            module=mod,
            sibling_module_ids=sibling_ids,
        )
        work_id = f"learning-{mod['id']}"
        prompt_path = prompt_dir / f"{work_id}.md"
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
        try:
            result = run_agent_command(formatted, cwd=learning_path)
        except AgentLimitReached:
            deferred = mod["id"]
            break
        if result.limit_reached:
            deferred = mod["id"]
            break
        if result.returncode == 0:
            generated.append(mod["id"])
        elif module_is_authored(learning_path / "lessons" / mod["id"]):
            # The agent wrote the chapters, then hit the hard time limit
            # (exit 124/137) while wrapping up. The content is there; any
            # chapter cut short is picked up by the depth audit's thin_module.
            generated.append(mod["id"])
            timed_out.append(mod["id"])
        else:
            failed.append(mod["id"])

    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "generate_role_learning_content",
        "role_id": role_id,
        "learning_repo": role.learning_repo,
        "modules_generated": generated,
        "modules_failed": failed,
        "modules_skipped": skipped,
        "modules_timed_out": timed_out,
        "deferred_module": deferred,
    }
    write_json(state_root / LEARNING_CONTENT_REPORT, report)
    return report
