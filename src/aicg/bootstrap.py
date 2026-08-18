"""Scaffold a new role's learning + solutions repos.

A new role is two paired repos plus a manifest entry. This module
generates the skeleton on disk so subsequent ``aicg`` cycles
(research -> plan -> generate -> verify -> PR -> steward) can fill
the curriculum in without any further human bootstrapping.

Repo names + branding are domain-aware: they come from the manifest
(``<id>-learning`` for a sibling org, ``ai-infra-<id>-learning`` for
ai-infra), so bootstrap works across the whole curriculum-org family.

Phase A (this module) writes:

- ``<learning-repo>/``: README.md, LICENSE, .gitignore, CI workflow,
  CURRICULUM.md placeholder, empty ``lessons/`` and ``projects/``
  directories.
- ``<solutions-repo>/``: README.md, LICENSE, .gitignore, CI workflow,
  ``modules/`` and ``projects/`` directories, plus ``SOLUTIONS_INDEX.md``.
- A role entry appended to the org manifest (if the manifest is JSON
  the file is rewritten; YAML is left for the user to update
  manually with the printed snippet).
- A prompt packet at ``<state-dir>/bootstrap/<id>.md`` that asks the
  content agent to (a) research the role's job requirements and (b)
  draft the initial curriculum plan as
  ``<learning-repo>/.aicg/curriculum-plan.json``.

Phase B (out of scope for now): execute the curriculum plan, create
per-module skeletons, mark them as ready in the org queue.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .org_config import OrgManifest, state_dir_for_manifest
from .state import utc_now, write_json

BOOTSTRAP_REPORT = "bootstrap-report.json"
EXECUTE_PLAN_REPORT = "execute-plan-report.json"

_ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_MOD_ID_RE = re.compile(r"^mod-\d{2,4}(?:-[a-z0-9-]+)?$")
_EXERCISE_ID_RE = re.compile(r"^exercise-\d{2,3}$")
_PROJECT_ID_RE = re.compile(r"^project-\d{1,4}(?:-[a-z0-9-]+)?$")


class BootstrapError(RuntimeError):
    """Raised when a role cannot be scaffolded."""


@dataclass(frozen=True)
class BootstrapPlan:
    role_id: str
    title: str
    level: int
    learning_repo: str
    solution_repo: str | None
    learning_path: Path
    solution_path: Path | None
    prompt_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "title": self.title,
            "level": self.level,
            "learning_repo": self.learning_repo,
            "solution_repo": self.solution_repo,
            "learning_path": str(self.learning_path),
            "solution_path": str(self.solution_path) if self.solution_path else None,
            "prompt_path": str(self.prompt_path),
        }


def bootstrap_role(
    manifest: OrgManifest,
    workspace: Path,
    role_id: str,
    title: str,
    level: int,
    description: str | None = None,
    overwrite: bool = False,
    write_manifest: bool = True,
    create_remotes: bool = False,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Scaffold a paired learning + solutions repo set for ``role_id``."""
    if not _ROLE_ID_RE.match(role_id):
        raise BootstrapError(
            f"Invalid role id {role_id!r}; expected lowercase alphanumeric "
            "with hyphens (e.g. 'data-engineer')."
        )

    learning_repo, solution_repo = _resolve_repo_names(manifest, role_id)
    is_single = solution_repo is None
    org = manifest.org
    org_display = _org_display(org)
    site_banner = _site_banner(org)
    maintained_footer = _maintained_footer(manifest)
    workspace = workspace.resolve()
    learning_path = workspace / learning_repo
    solution_path = None if is_single else workspace / solution_repo

    if not overwrite:
        candidate_paths = [learning_path] if is_single else [learning_path, solution_path]
        for path in candidate_paths:
            if path.exists() and any(path.iterdir()):
                raise BootstrapError(
                    f"Target path already exists with content: {path}. "
                    "Pass overwrite=True to scaffold on top."
                )

    learning_path.mkdir(parents=True, exist_ok=True)
    if solution_path is not None:
        solution_path.mkdir(parents=True, exist_ok=True)

    description = description or f"Curriculum for the {title} role."
    files_written: list[str] = []

    ctx = _SkeletonContext(
        org=org,
        org_display=org_display,
        learning_repo=learning_repo,
        # Single-repo curricula have no separate solutions repo; point the
        # skeleton's cross-links back at the one curriculum repo.
        solution_repo=solution_repo or learning_repo,
        site_banner=site_banner,
        maintained_footer=maintained_footer,
        is_single=is_single,
    )
    if is_single:
        files_written.extend(
            _write_single_skeleton(learning_path, role_id, title, description, ctx)
        )
    else:
        files_written.extend(
            _write_learning_skeleton(learning_path, role_id, title, description, ctx)
        )
        files_written.extend(
            _write_solutions_skeleton(solution_path, role_id, title, description, ctx)
        )

    state_root = state_dir_for_manifest(manifest, state_dir)
    bootstrap_dir = state_root / "bootstrap"
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = bootstrap_dir / f"{role_id}.md"
    prompt_path.write_text(
        _build_bootstrap_prompt(
            role_id, title, level, description, learning_repo, solution_repo
        ),
        encoding="utf-8",
    )

    plan = BootstrapPlan(
        role_id=role_id,
        title=title,
        level=level,
        learning_repo=learning_repo,
        solution_repo=solution_repo,
        learning_path=learning_path,
        solution_path=solution_path,
        prompt_path=prompt_path,
    )

    manifest_update = None
    if write_manifest:
        manifest_update = _append_role_to_manifest(manifest, plan)

    remote_result: dict[str, Any] | None = None
    if create_remotes:
        remote_result = _create_github_repos(manifest, plan)

    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "bootstrap_role",
        "plan": plan.to_dict(),
        "files_written": files_written,
        "manifest_update": manifest_update,
        "remotes": remote_result,
    }
    write_json(state_root / BOOTSTRAP_REPORT, report)
    return report


# ---------------------------------------------------------------------------
# Domain-aware naming + branding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SkeletonContext:
    """Per-domain branding threaded through every skeleton builder.

    Keeps bootstrap output correct across the org family: repo names and
    URLs come from the manifest (``ml-engineer-learning`` for a sibling
    org, ``ai-infra-data-engineer-learning`` for ai-infra), not a
    hardcoded ``ai-infra-`` prefix.
    """

    org: str
    org_display: str
    learning_repo: str
    solution_repo: str
    site_banner: str
    maintained_footer: str
    # True for single-repo curricula (no separate solutions repo; exemplars
    # live in-repo). Threaded into the README so the layout/links are correct.
    is_single: bool = False


# Display names for the curriculum-family orgs. Falls back to a title-cased
# slug for any future org so a new domain still renders a sane heading.
_ORG_DISPLAY = {
    "ai-infra": "AI Infrastructure",
    "ai-engineering": "AI Engineering",
    "ai-governance": "AI Governance",
    "ml-engineering": "ML Engineering",
}


def _org_base(org: str) -> str:
    base = org
    if base.lower().endswith("-curriculum"):
        base = base[: -len("-curriculum")]
    return base


def _org_display(org: str) -> str:
    base = _org_base(org)
    return _ORG_DISPLAY.get(base.lower(), base.replace("-", " ").title())


def _resolve_repo_names(manifest: OrgManifest, role_id: str) -> tuple[str, str | None]:
    """Repo names for ``role_id`` — from the manifest if present, else by
    the org's naming convention (ai-infra keeps its legacy prefix; sibling
    orgs drop it). ``solution_repo`` is ``None`` for single-repo curricula."""
    for role in manifest.roles:
        if role.id == role_id:
            return role.learning_repo, role.solution_repo
    if manifest.org.lower() == "ai-infra-curriculum":
        return f"ai-infra-{role_id}-learning", f"ai-infra-{role_id}-solutions"
    return f"{role_id}-learning", f"{role_id}-solutions"


def _site_banner(org: str) -> str:
    """The unified AI Career Curriculum ecosystem banner — same across all four
    orgs (it establishes the federation and keeps the only live landing site,
    ai-infra's cohorts/teams, reachable). Kept in sync with
    scripts/apply-ecosystem-banner.py."""
    return (
        "<!-- aicg:site-banner -->\n"
        "> 🎓 Part of the free, open-source **AI Career Curriculum** ecosystem — "
        "[Infrastructure](https://github.com/ai-infra-curriculum) · "
        "[ML Engineering](https://github.com/ml-engineering-curriculum) · "
        "[AI Engineering](https://github.com/ai-engineering-curriculum) · "
        "[Governance](https://github.com/ai-governance-curriculum) · "
        "[Startup](https://github.com/ai-startup-curriculum). "
        "Live cohorts &amp; team programs: "
        "**[ai-infra-curriculum.github.io](https://ai-infra-curriculum.github.io/)**.\n"
        "<!-- /aicg:site-banner -->\n\n"
    )


def write_bootstrap_prompt(
    manifest: OrgManifest,
    role_id: str,
    state_dir: Path | None = None,
) -> Path:
    """Write only the bootstrap research+plan prompt for a role (no scaffold).

    Used by the seeding flow to (re)generate a role's prompt packet on the
    runner without touching the repo's existing files (READMEs, banners). The
    role must already be in the manifest. Returns the prompt path.
    """
    role = next((r for r in manifest.roles if r.id == role_id), None)
    if role is None:
        raise BootstrapError(f"role {role_id!r} not in manifest {manifest.org}")
    description = f"Curriculum for the {role.title} role."
    state_root = state_dir_for_manifest(manifest, state_dir)
    bootstrap_dir = state_root / "bootstrap"
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = bootstrap_dir / f"{role_id}.md"
    prompt_path.write_text(
        _build_bootstrap_prompt(
            role_id, role.title, role.level, description,
            role.learning_repo, role.solution_repo,
        ),
        encoding="utf-8",
    )
    return prompt_path


def _maintained_footer(manifest: OrgManifest) -> str:
    mb = manifest.maintained_by or {}
    marker = mb.get("footer_marker", "<!-- aicg:maintained-by -->")
    phrasing = mb.get("phrasing")
    if not phrasing:
        name = mb.get("name")
        url = mb.get("url")
        phrasing = f"Maintained by [{name}]({url})" if name and url else ""
    if not phrasing:
        return ""
    return f"\n---\n\n{marker}\n{phrasing}\n"


# ---------------------------------------------------------------------------
# Skeleton writers
# ---------------------------------------------------------------------------


def _write_learning_skeleton(
    repo_path: Path, role_id: str, title: str, description: str, ctx: _SkeletonContext
) -> list[str]:
    written: list[str] = []
    written.append(
        _write(
            repo_path / "README.md",
            _learning_readme(title, description, ctx),
        )
    )
    written.append(_write(repo_path / "LICENSE", _mit_license(ctx)))
    written.append(_write(repo_path / ".gitignore", _gitignore()))
    written.append(
        _write(
            repo_path / "CURRICULUM.md",
            _learning_curriculum_placeholder(role_id, title),
        )
    )
    written.append(
        _write(
            repo_path / "PREREQUISITES.md",
            _learning_prerequisites_placeholder(title),
        )
    )
    written.append(
        _write(
            repo_path / "VERSIONS.md",
            _versions_placeholder(ctx),
        )
    )
    written.append(
        _write(
            repo_path / ".github" / "workflows" / "ci.yml",
            _learning_ci_workflow(),
        )
    )
    written.append(
        _write(
            repo_path / ".markdownlint.jsonc",
            _markdownlint_config(),
        )
    )
    # Empty content directories with placeholder READMEs so they show
    # up under git.
    written.append(
        _write(
            repo_path / "lessons" / "README.md",
            "# Lessons\n\nModule directories live here as `lessons/mod-XXX-name/`.\n",
        )
    )
    written.append(
        _write(
            repo_path / "projects" / "README.md",
            "# Projects\n\nCapstones live here as `projects/project-XXX-name/`.\n",
        )
    )
    return written


def _write_single_skeleton(
    repo_path: Path, role_id: str, title: str, description: str, ctx: _SkeletonContext
) -> list[str]:
    """Scaffold a single ``*-curriculum`` repo (startup-domain model).

    Reuses the learning skeleton (lessons/projects/CURRICULUM/CI/etc.) and adds
    an ``exemplars/`` tree where worked founder artifacts (cap tables, runway
    models, pitch narratives) live in-repo — there is no separate solutions repo.
    """
    written = _write_learning_skeleton(repo_path, role_id, title, description, ctx)
    written.append(
        _write(
            repo_path / "exemplars" / "README.md",
            "# Exemplars\n\nWorked founder artifacts for this curriculum's "
            "exercises live here as `exemplars/mod-XXX-name/`.\n\n"
            "These are reference deliverables (filled cap tables, runway models, "
            "investor funnels, pitch narratives), not code solutions.\n",
        )
    )
    return written


def _write_solutions_skeleton(
    repo_path: Path, role_id: str, title: str, description: str, ctx: _SkeletonContext
) -> list[str]:
    written: list[str] = []
    written.append(
        _write(
            repo_path / "README.md",
            _solutions_readme(title, description, ctx),
        )
    )
    written.append(_write(repo_path / "LICENSE", _mit_license(ctx)))
    written.append(_write(repo_path / ".gitignore", _gitignore_solutions()))
    written.append(
        _write(
            repo_path / "SOLUTIONS_INDEX.md",
            _solutions_index_placeholder(title, ctx),
        )
    )
    written.append(
        _write(
            repo_path / ".github" / "workflows" / "ci.yml",
            _solutions_ci_workflow(),
        )
    )
    written.append(
        _write(
            repo_path / ".markdownlint.jsonc",
            _markdownlint_config(),
        )
    )
    written.append(
        _write(
            repo_path / "modules" / "README.md",
            "# Modules\n\nReference solutions per module live here.\n",
        )
    )
    written.append(
        _write(
            repo_path / "projects" / "README.md",
            "# Projects\n\nReference solutions per project live here.\n",
        )
    )
    return written


def _markdownlint_config() -> str:
    """Lint config for curriculum repos.

    The CI workflow runs markdownlint over generated lecture/exercise/solution
    prose. Default markdownlint enforces stylistic rules (line length, fenced-
    code-language, etc.) that are hostile to heterogeneous AI-authored content,
    so we disable the style rules and keep only the broken-markdown checks
    (empty links, malformed tables, spaced code spans). Mirrors the config used
    by the sibling guidebook and existing curriculum repos.
    """
    return """\
// markdownlint-cli2 config for curriculum content.
//
// Lint catches *broken* markdown (malformed tables, empty links, spaced code
// spans) but does NOT enforce stylistic preferences — strict style is hostile
// to heterogeneous AI-authored content. Rule reference:
// https://github.com/DavidAnson/markdownlint/blob/main/doc/Rules.md
{
  "default": true,

  // === Disabled: style preferences ===
  "MD001": false,  // heading-increment
  "MD007": false,  // ul-indent style
  "MD009": false,  // trailing-spaces
  "MD010": false,  // hard-tabs (occasional in code blocks)
  "MD012": false,  // multiple-blanks
  "MD013": false,  // line-length (prose wraps as written)
  "MD014": false,  // commands-show-output
  "MD018": false,  // no-missing-space-atx
  "MD019": false,  // no-multiple-space-atx
  "MD022": false,  // blanks-around-headings
  "MD024": false,  // duplicate-heading
  "MD025": false,  // multiple-h1
  "MD026": false,  // trailing-punctuation in headings
  "MD028": false,  // no-blanks-blockquote
  "MD029": false,  // ol-prefix style
  "MD031": false,  // blanks-around-fences
  "MD032": false,  // blanks-around-lists
  "MD033": false,  // inline-html (needs-research markers, badges)
  "MD034": false,  // bare-URL
  "MD035": false,  // hr-style
  "MD036": false,  // emphasis-as-heading
  "MD037": false,  // no-space-in-emphasis
  "MD040": false,  // fenced-code-language
  "MD041": false,  // first-line-h1
  "MD046": false,  // code-block-style
  "MD047": false,  // single-trailing-newline
  "MD050": false,  // strong-style
  "MD051": false,  // link-fragments
  "MD058": false,  // blanks-around-tables
  "MD060": false   // table-pipe-spacing

  // Broken-markdown checks remain ON: MD038 (no-space-in-code-spans),
  // MD039 (no-space-in-links), MD042 (no-empty-links), MD056
  // (table-column-count), and the rest of the defaults.
}
"""


def _write(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# Skeleton content
# ---------------------------------------------------------------------------


def _learning_readme(title: str, description: str, ctx: _SkeletonContext) -> str:
    if ctx.is_single:
        heading = f"# {ctx.org_display} · {title} — Curriculum\n\n"
        layout = (
            "## Layout\n\n"
            "```\n"
            f"{ctx.learning_repo}/\n"
            "├── lessons/mod-XXX-*/        modules with lectures, exercises, labs, quizzes\n"
            "├── exemplars/mod-XXX-*/      worked reference deliverables (not code solutions)\n"
            "├── projects/project-XXX-*/   multi-module capstones\n"
            "├── CURRICULUM.md             role-level coverage map\n"
            "├── PREREQUISITES.md          assumed entry skills\n"
            "├── VERSIONS.md               release history\n"
            "└── README.md                 this file\n"
            "```\n\n"
        )
        last_section = (
            "## Exemplars\n\n"
            "This is a single-repo curriculum: worked reference deliverables live "
            "in-repo under [`exemplars/`](./exemplars), not in a separate solutions repo.\n"
        )
        return f"{heading}{ctx.site_banner}{description}\n\n" + (
            "> **Status**: scaffolded by `aicg org bootstrap-role`. The curriculum is "
            "not authored yet. Run `aicg org research` and `aicg org daily` to drive "
            "the autonomous fill-in loop.\n\n"
        ) + layout + last_section + ctx.maintained_footer

    sol_url = f"https://github.com/{ctx.org}/{ctx.solution_repo}"
    return (
        f"# {ctx.org_display} · {title} — Learning Repository\n\n"
        f"{ctx.site_banner}"
        f"{description}\n\n"
        "> **Status**: scaffolded by `aicg org bootstrap-role`. The curriculum is "
        "not authored yet. Run `aicg org research` and `aicg org daily` to drive "
        "the autonomous fill-in loop.\n\n"
        "## Layout\n\n"
        "```\n"
        f"{ctx.learning_repo}/\n"
        "├── lessons/mod-XXX-*/        modules with lectures, exercises, labs, quizzes\n"
        "├── projects/project-XXX-*/   multi-module capstones\n"
        "├── CURRICULUM.md             role-level coverage map\n"
        "├── PREREQUISITES.md          assumed entry skills\n"
        "├── VERSIONS.md               release history\n"
        "└── README.md                 this file\n"
        "```\n\n"
        "## Paired Solutions Repo\n\n"
        f"[`{ctx.solution_repo}`]({sol_url}) "
        "carries the reference implementations.\n"
        f"{ctx.maintained_footer}"
    )


def _solutions_readme(title: str, description: str, ctx: _SkeletonContext) -> str:
    learn_url = f"https://github.com/{ctx.org}/{ctx.learning_repo}"
    return (
        f"# {ctx.org_display} · {title} — Solutions Repository\n\n"
        f"{ctx.site_banner}"
        "Reference implementations for the paired "
        f"[`{ctx.learning_repo}`]({learn_url}) "
        "track.\n\n"
        "> **Status**: scaffolded by `aicg org bootstrap-role`. Module and "
        "project solutions arrive over subsequent autonomous cycles.\n\n"
        "## Layout\n\n"
        "```\n"
        f"{ctx.solution_repo}/\n"
        "├── modules/mod-XXX-*/                 module-level rationale + per-exercise solutions\n"
        "├── projects/project-XXX-*/            capstone walkthroughs\n"
        "├── SOLUTIONS_INDEX.md                 inventory + completion map\n"
        "└── README.md                          this file\n"
        "```\n"
        f"{ctx.maintained_footer}"
    )


def _learning_curriculum_placeholder(role_id: str, title: str) -> str:
    return (
        f"# {title} Curriculum\n\n"
        "> Scaffolded placeholder. The autonomous loop will populate this file "
        "as modules are designed and shipped.\n\n"
        "## Overview\n\n"
        "TBD.\n\n"
        "## Module Plan\n\n"
        "| Module | Status |\n"
        "|---|---|\n"
        "| mod-XXX | planned |\n"
    )


def _learning_prerequisites_placeholder(title: str) -> str:
    return (
        f"# Prerequisites for the {title} track\n\n"
        "> Scaffolded placeholder. Update during the curriculum-planning step.\n"
    )


def _versions_placeholder(ctx: _SkeletonContext) -> str:
    return (
        f"# Versions — {ctx.learning_repo}\n\n"
        "| Tag | Date | Highlights |\n"
        "|---|---|---|\n"
        "| (unreleased) | TBD | initial scaffold |\n"
    )


def _solutions_index_placeholder(title: str, ctx: _SkeletonContext) -> str:
    return (
        f"# Solutions Index — {title}\n\n"
        f"Reference implementations for `{ctx.learning_repo}`.\n\n"
        "## Coverage\n\n"
        "| Module | Solution Status |\n"
        "|---|---|\n"
        "| mod-XXX | planned |\n"
    )


def _mit_license(ctx: _SkeletonContext) -> str:
    return (
        "MIT License\n\n"
        f"Copyright (c) 2026 {ctx.org_display} Curriculum\n\n"
        "Permission is hereby granted, free of charge, to any person obtaining a copy "
        'of this software and associated documentation files (the "Software"), to deal '
        "in the Software without restriction, including without limitation the rights "
        "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell "
        "copies of the Software, and to permit persons to whom the Software is "
        "furnished to do so, subject to the following conditions:\n\n"
        "The above copyright notice and this permission notice shall be included in "
        "all copies or substantial portions of the Software.\n\n"
        'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR '
        "IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, "
        "FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE "
        "AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER "
        "LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING "
        "FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS "
        "IN THE SOFTWARE.\n"
    )


def _gitignore() -> str:
    return (
        "__pycache__/\n"
        "*.py[cod]\n"
        ".venv/\n"
        "venv/\n"
        ".DS_Store\n"
        "# AICG runner state — never commit.\n"
        ".aicg/\n"
    )


def _gitignore_solutions() -> str:
    # Same content for the solutions side but explicit so future
    # additions (e.g. wheel artefacts) can diverge cleanly.
    return _gitignore()


def _learning_ci_workflow() -> str:
    return (
        "name: CI\n\n"
        "on:\n"
        "  pull_request:\n"
        "    branches: [main]\n"
        "  push:\n"
        "    branches: [main]\n\n"
        "permissions:\n"
        "  contents: read\n\n"
        "jobs:\n"
        "  markdown-lint:\n"
        "    name: Markdown lint\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: DavidAnson/markdownlint-cli2-action@v16\n"
        "        with:\n"
        "          globs: |\n"
        "            lessons/**/*.md\n"
        "            projects/**/*.md\n"
        "            !**/node_modules/**\n"
    )


def _solutions_ci_workflow() -> str:
    return (
        "name: CI\n\n"
        "on:\n"
        "  pull_request:\n"
        "    branches: [main]\n"
        "  push:\n"
        "    branches: [main]\n\n"
        "permissions:\n"
        "  contents: read\n\n"
        "jobs:\n"
        "  markdown-lint:\n"
        "    name: Markdown lint\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: DavidAnson/markdownlint-cli2-action@v16\n"
        "        with:\n"
        "          globs: |\n"
        "            modules/**/*.md\n"
        "            projects/**/*.md\n"
        "            !**/node_modules/**\n\n"
        "  python-syntax:\n"
        "    name: Python syntax (modules/)\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n"
        "          python-version: \"3.10\"\n"
        "      - name: Compile every .py under modules/\n"
        "        shell: bash\n"
        "        run: |\n"
        "          set -euo pipefail\n"
        "          shopt -s globstar nullglob\n"
        "          files=( modules/**/*.py )\n"
        "          if [ ${#files[@]} -eq 0 ]; then\n"
        "            echo \"No modules/**/*.py — nothing to check.\"\n"
        "            exit 0\n"
        "          fi\n"
        "          fails=0\n"
        "          for f in \"${files[@]}\"; do\n"
        "            if ! python -m py_compile \"$f\"; then\n"
        "              echo \"::error file=$f::SyntaxError\"\n"
        "              fails=$((fails+1))\n"
        "            fi\n"
        "          done\n"
        "          echo \"Compiled ${#files[@]} file(s); ${fails} failure(s).\"\n"
        "          exit \"$fails\"\n"
    )


# ---------------------------------------------------------------------------
# Manifest update + remote creation
# ---------------------------------------------------------------------------


def _append_role_to_manifest(
    manifest: OrgManifest, plan: BootstrapPlan
) -> dict[str, Any]:
    if plan.role_id in {role.id for role in manifest.roles}:
        return {
            "status": "already_present",
            "manifest_path": str(manifest.path),
        }

    text = manifest.path.read_text(encoding="utf-8")
    new_role = {
        "id": plan.role_id,
        "title": plan.title,
        "level": plan.level,
        "learning_repo": plan.learning_repo,
        "solution_repo": plan.solution_repo,
    }

    stripped = text.lstrip()
    if stripped.startswith("{"):
        # JSON manifest — rewrite in place.
        payload = json.loads(text)
        payload.setdefault("roles", []).append(new_role)
        manifest.path.write_text(
            json.dumps(payload, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        return {
            "status": "appended_json",
            "manifest_path": str(manifest.path),
        }

    # YAML manifest — the runner does not commit to rewriting arbitrary
    # YAML in-place. Print the snippet for the operator to paste.
    return {
        "status": "yaml_manifest_manual_update_required",
        "manifest_path": str(manifest.path),
        "snippet": (
            "  - id: " + plan.role_id + "\n"
            "    title: " + json.dumps(plan.title) + "\n"
            "    level: " + str(plan.level) + "\n"
            "    learning_repo: " + plan.learning_repo + "\n"
            "    solution_repo: " + plan.solution_repo + "\n"
        ),
    }


def _git_init_commit(repo_path: Path) -> subprocess.CompletedProcess | None:
    """Init a git repo + initial commit so ``gh repo create --push`` works.

    Returns None on success, or the first failed CompletedProcess. Skips
    init when ``.git`` already exists; skips commit when nothing changed.
    """
    steps: list[list[str]] = []
    if not (repo_path / ".git").is_dir():
        steps.append(["git", "init", "-b", "main"])
    steps.append(["git", "add", "-A"])
    for step in steps:
        proc = subprocess.run(
            step, cwd=str(repo_path), capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            return proc
    # Commit only when there's something staged (idempotent on re-run).
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if staged.returncode != 0:
        proc = subprocess.run(
            ["git", "commit", "-m", "chore: initial scaffold via aicg org bootstrap-role"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return proc
    return None


def _create_github_repos(
    manifest: OrgManifest, plan: BootstrapPlan
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    repo_targets: list[tuple[str, Path]] = [(plan.learning_repo, plan.learning_path)]
    if plan.solution_repo and plan.solution_path is not None:
        repo_targets.append((plan.solution_repo, plan.solution_path))
    branch = (manifest.release or {}).get("branch") or "main"
    for repo_name, repo_path in repo_targets:
        # gh repo create needs an initialized git repo with a commit; the
        # scaffold is just files on disk, so init + commit first. Idempotent:
        # skip init if already a repo.
        init_err = _git_init_commit(repo_path)
        if init_err is not None:
            results.append(
                {
                    "repo": repo_name,
                    "step": "git_init",
                    "returncode": init_err.returncode,
                    "stdout": init_err.stdout[-2000:],
                    "stderr": init_err.stderr[-2000:],
                }
            )
            continue

        # Create the empty remote. We do NOT use `gh repo create --push`: it
        # has proved unreliable (creates the repo but silently leaves it empty),
        # so the explicit `git push` below is the source of truth. Re-runs where
        # the repo already exists are treated as success and fall through to the
        # push, which makes the whole operation idempotent.
        created = subprocess.run(
            ["gh", "repo", "create", f"{manifest.org}/{repo_name}", "--public"],
            capture_output=True,
            text=True,
            check=False,
        )
        already_exists = "already exists" in (created.stderr or "").lower()
        if created.returncode != 0 and not already_exists:
            results.append(
                {
                    "repo": repo_name,
                    "step": "gh_create",
                    "returncode": created.returncode,
                    "stdout": created.stdout[-2000:],
                    "stderr": created.stderr[-2000:],
                }
            )
            continue

        # Point origin at the repo over gh's authenticated HTTPS (works on any
        # host where gh is logged in, unlike the manifest's SSH default_remote).
        remote_url = f"https://github.com/{manifest.org}/{repo_name}.git"
        subprocess.run(
            ["git", "remote", "remove", "origin"],
            cwd=str(repo_path), capture_output=True, text=True, check=False,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", remote_url],
            cwd=str(repo_path), capture_output=True, text=True, check=False,
        )
        # The explicit, verifiable push. Idempotent: a no-op if already pushed.
        pushed = subprocess.run(
            ["git", "push", "-u", "origin", f"HEAD:{branch}"],
            cwd=str(repo_path), capture_output=True, text=True, check=False,
        )
        results.append(
            {
                "repo": repo_name,
                "step": "push" if pushed.returncode != 0 else "ok",
                "returncode": pushed.returncode,
                "created": created.returncode == 0,
                "already_existed": already_exists,
                "stdout": pushed.stdout[-2000:],
                "stderr": pushed.stderr[-2000:],
            }
        )
    return {"actions": results}


# ---------------------------------------------------------------------------
# Bootstrap prompt
# ---------------------------------------------------------------------------


def _build_bootstrap_prompt(
    role_id: str,
    title: str,
    level: int,
    description: str,
    learning_repo: str,
    solution_repo: str,
) -> str:
    return (
        f"# Bootstrap Curriculum Packet — {title} (level {level})\n\n"
        f"`{learning_repo}` and `{solution_repo}` were "
        "scaffolded with empty curriculum directories. Your job is to research "
        "the role and produce the initial curriculum plan.\n\n"
        "## Phase 1 — Research\n\n"
        "- Analyse at least 25 current job postings for this role.\n"
        f"- Description seed: {description}\n"
        "- Capture employer, posting URL, date, location, required skills, "
        "preferred skills, salary range.\n"
        "- Normalise findings into "
        f"`{learning_repo}/.aicg/job-requirements.json`.\n"
        "- Write a readable summary to "
        f"`{learning_repo}/JOB_REQUIREMENTS.md`.\n"
        "- Cite official sources where claims need backing.\n\n"
        "## Phase 2 — Curriculum Plan\n\n"
        "Author "
        f"`{learning_repo}/.aicg/curriculum-plan.json` with this shape:\n\n"
        "```json\n"
        "{\n"
        "  \"schema_version\": 1,\n"
        "  \"role_id\": \"" + role_id + "\",\n"
        "  \"title\": \"" + title + "\",\n"
        "  \"level\": " + str(level) + ",\n"
        "  \"modules\": [\n"
        "    {\n"
        "      \"id\": \"mod-101-foundations\",\n"
        "      \"title\": \"...\",\n"
        "      \"hours\": 12,\n"
        "      \"objectives\": [\"...\"],\n"
        "      \"exercises\": [\n"
        "        {\"id\": \"exercise-01\", \"slug\": \"...\", \"hours\": 2}\n"
        "      ],\n"
        "      \"labs\": [...],\n"
        "      \"quizzes\": 1\n"
        "    }\n"
        "  ],\n"
        "  \"projects\": [\n"
        "    {\"id\": \"project-101-...\", \"title\": \"...\", \"hours\": 20}\n"
        "  ]\n"
        "}\n"
        "```\n\n"
        "## Ownership Rule\n\n"
        "If a requirement is already covered by a lower-level role, link to "
        "that owner instead of duplicating. Higher-level coverage should add "
        "depth, architectural context, or leadership framing — not repeat the "
        "fundamentals.\n\n"
        "## Constraints\n\n"
        "- Do not invent facts, incidents, or salary figures. Cite sources.\n"
        "- Unverified claims must use `<!-- needs-research: ... -->`.\n"
        "- Preserve the standard layout (`lessons/mod-XXX-*/` for learning, "
        "`modules/mod-XXX-*/` for solutions).\n"
        "- Update `CURRICULUM.md`, `PREREQUISITES.md`, `VERSIONS.md`, "
        "`README.md` to reflect the planned curriculum once `curriculum-plan.json` "
        "lands.\n"
    )


# ---------------------------------------------------------------------------
# Phase B: execute a curriculum-plan.json into module skeletons
# ---------------------------------------------------------------------------


class CurriculumPlanError(BootstrapError):
    """Raised when a curriculum-plan.json is missing or malformed."""


@dataclass(frozen=True)
class ExecutePlanResult:
    role_id: str
    learning_path: Path
    solution_path: Path
    modules_created: list[str]
    modules_skipped: list[str]
    projects_created: list[str]
    projects_skipped: list[str]
    files_written: list[str]


def execute_curriculum_plan(
    manifest: OrgManifest,
    workspace: Path,
    role_id: str,
    plan_path: Path | None = None,
    overwrite: bool = False,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Read ``curriculum-plan.json`` and scaffold module + project skeletons."""
    if not _ROLE_ID_RE.match(role_id):
        raise CurriculumPlanError(f"Invalid role id {role_id!r}.")

    role = next((item for item in manifest.roles if item.id == role_id), None)
    if role is None:
        raise CurriculumPlanError(
            f"Role {role_id!r} not in manifest. Run `aicg org bootstrap-role` first."
        )

    workspace = workspace.resolve()
    learning_path = workspace / role.learning_repo
    solution_path = workspace / role.solution_repo
    if not learning_path.exists() or not solution_path.exists():
        raise CurriculumPlanError(
            f"Paired repos for {role_id!r} are not on disk. Bootstrap them first."
        )

    plan_path = plan_path or learning_path / ".aicg" / "curriculum-plan.json"
    if not plan_path.exists():
        raise CurriculumPlanError(
            f"Curriculum plan not found at {plan_path}. Author it first "
            "(see the bootstrap prompt)."
        )

    plan = _load_curriculum_plan(plan_path)
    if plan.get("role_id") and plan["role_id"] != role_id:
        raise CurriculumPlanError(
            f"Plan role_id {plan.get('role_id')!r} does not match {role_id!r}."
        )

    files_written: list[str] = []
    modules_created: list[str] = []
    modules_skipped: list[str] = []
    projects_created: list[str] = []
    projects_skipped: list[str] = []

    for module in plan.get("modules", []) or []:
        result = _scaffold_module(
            module=module,
            learning_path=learning_path,
            solution_path=solution_path,
            overwrite=overwrite,
        )
        files_written.extend(result["files_written"])
        if result["created"]:
            modules_created.append(result["module_id"])
        else:
            modules_skipped.append(result["module_id"])

    for project in plan.get("projects", []) or []:
        result = _scaffold_project(
            project=project,
            learning_path=learning_path,
            solution_path=solution_path,
            overwrite=overwrite,
        )
        files_written.extend(result["files_written"])
        if result["created"]:
            projects_created.append(result["project_id"])
        else:
            projects_skipped.append(result["project_id"])

    state_root = state_dir_for_manifest(manifest, state_dir)
    state_root.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "execute_curriculum_plan",
        "role_id": role_id,
        "plan_path": str(plan_path),
        "learning_path": str(learning_path),
        "solution_path": str(solution_path),
        "modules_created": modules_created,
        "modules_skipped": modules_skipped,
        "projects_created": projects_created,
        "projects_skipped": projects_skipped,
        "files_written_count": len(files_written),
        "files_written_sample": files_written[:20],
    }
    write_json(state_root / EXECUTE_PLAN_REPORT, report)
    return report


def _load_curriculum_plan(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CurriculumPlanError(f"Could not parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CurriculumPlanError(f"{path} must be a JSON object.")
    if not isinstance(data.get("modules"), list):
        raise CurriculumPlanError(f"{path} requires a `modules` array.")
    return data


def _scaffold_module(
    module: dict[str, Any],
    learning_path: Path,
    solution_path: Path,
    overwrite: bool,
) -> dict[str, Any]:
    module_id = module.get("id") or ""
    if not _MOD_ID_RE.match(module_id):
        raise CurriculumPlanError(
            f"Module id {module_id!r} must match `mod-NNN[-slug]`."
        )
    title = module.get("title") or module_id
    objectives = module.get("objectives") or []
    exercises = module.get("exercises") or []
    labs = module.get("labs") or []
    hours = module.get("hours")

    learning_mod = learning_path / "lessons" / module_id
    solution_mod = solution_path / "modules" / module_id

    files_written: list[str] = []
    created = not learning_mod.exists() and not solution_mod.exists()
    if not created and not overwrite:
        return {
            "module_id": module_id,
            "created": False,
            "files_written": [],
        }

    files_written.append(
        _write(
            learning_mod / "README.md",
            _module_learning_readme(module_id, title, objectives, hours),
        )
    )
    files_written.append(
        _write(
            learning_mod / "resources.md",
            _module_resources_placeholder(module_id, title),
        )
    )
    files_written.append(
        _write(
            learning_mod / "quizzes" / "README.md",
            f"# {title} quizzes\n\nAuthored under the autonomous fill-in loop.\n",
        )
    )
    files_written.append(
        _write(
            learning_mod / "labs" / "README.md",
            _module_labs_placeholder(module_id, title, labs),
        )
    )

    for exercise in exercises:
        exercise_id = exercise.get("id") or ""
        slug = exercise.get("slug") or ""
        if not _EXERCISE_ID_RE.match(exercise_id):
            raise CurriculumPlanError(
                f"Exercise id {exercise_id!r} must match `exercise-NN`."
            )
        if slug and not re.match(r"^[a-z0-9-]+$", slug):
            raise CurriculumPlanError(
                f"Exercise slug {slug!r} must be lowercase-hyphen."
            )
        title_part = exercise.get("title") or slug.replace("-", " ").title() or exercise_id
        relative_name = f"{exercise_id}-{slug}.md" if slug else f"{exercise_id}.md"
        files_written.append(
            _write(
                learning_mod / "exercises" / relative_name,
                _exercise_learning_placeholder(
                    module_id, exercise_id, slug, title_part, exercise
                ),
            )
        )

    # Solutions side mirror.
    files_written.append(
        _write(
            solution_mod / "README.md",
            _module_solution_readme(module_id, title),
        )
    )
    for exercise in exercises:
        exercise_id = exercise.get("id") or ""
        slug = exercise.get("slug") or ""
        directory = (
            solution_mod / f"{exercise_id}-{slug}"
            if slug
            else solution_mod / exercise_id
        )
        files_written.append(
            _write(
                directory / "README.md",
                _exercise_solution_placeholder(module_id, exercise_id, slug),
            )
        )

    return {
        "module_id": module_id,
        "created": True,
        "files_written": files_written,
    }


def _scaffold_project(
    project: dict[str, Any],
    learning_path: Path,
    solution_path: Path,
    overwrite: bool,
) -> dict[str, Any]:
    project_id = project.get("id") or ""
    if not _PROJECT_ID_RE.match(project_id):
        raise CurriculumPlanError(
            f"Project id {project_id!r} must match `project-N[-slug]`."
        )
    title = project.get("title") or project_id
    hours = project.get("hours")

    learning_proj = learning_path / "projects" / project_id
    solution_proj = solution_path / "projects" / project_id

    files_written: list[str] = []
    created = not learning_proj.exists() and not solution_proj.exists()
    if not created and not overwrite:
        return {
            "project_id": project_id,
            "created": False,
            "files_written": [],
        }

    files_written.append(
        _write(
            learning_proj / "README.md",
            _project_learning_placeholder(project_id, title, hours, project),
        )
    )
    files_written.append(
        _write(
            solution_proj / "README.md",
            _project_solution_placeholder(project_id, title),
        )
    )

    return {
        "project_id": project_id,
        "created": True,
        "files_written": files_written,
    }


# ---------------------------------------------------------------------------
# Phase B content templates
# ---------------------------------------------------------------------------


def _module_learning_readme(
    module_id: str, title: str, objectives: list[str], hours: Any
) -> str:
    bullets = "\n".join(f"- {item}" for item in objectives) or "- TBD"
    duration = f"\n**Estimated effort:** {hours} hours\n" if hours else ""
    return (
        f"# {module_id}: {title}\n\n"
        "> Scaffolded by `aicg org execute-plan`. Lecture chapters and exercise "
        "content are authored on subsequent autonomous cycles.\n"
        f"{duration}"
        "\n## Learning objectives\n\n"
        f"{bullets}\n\n"
        "## Structure\n\n"
        "- `01-…md` … `0N-…md`: lecture chapters.\n"
        "- `exercises/`: per-exercise prompts.\n"
        "- `labs/`: long-form hands-on labs.\n"
        "- `quizzes/`: knowledge checks.\n"
        "- `resources.md`: external references.\n"
    )


def _module_solution_readme(module_id: str, title: str) -> str:
    return (
        f"# {module_id}: {title} — Solutions\n\n"
        "Reference implementations + per-exercise walkthroughs land here.\n\n"
        "Run `aicg audit --repo <this-repo>` to see which exercises still need "
        "solutions; `aicg org daily` will plan and queue work items.\n"
    )


def _module_resources_placeholder(module_id: str, title: str) -> str:
    return (
        f"# Resources for {module_id} ({title})\n\n"
        "> Scaffolded placeholder. Curated reading + tooling links land here.\n"
    )


def _module_labs_placeholder(
    module_id: str, title: str, labs: list[dict[str, Any]]
) -> str:
    body = f"# Labs for {module_id} ({title})\n\n"
    if labs:
        body += "Planned labs:\n\n"
        for lab in labs:
            lab_id = lab.get("id", "lab-XX")
            lab_title = lab.get("title", lab_id)
            body += f"- `{lab_id}` — {lab_title}\n"
    else:
        body += "Labs will be authored on subsequent cycles.\n"
    return body


def _exercise_learning_placeholder(
    module_id: str,
    exercise_id: str,
    slug: str,
    title: str,
    exercise: dict[str, Any],
) -> str:
    hours = exercise.get("hours")
    duration = f"**Estimated effort:** {hours} hours\n\n" if hours else ""
    return (
        f"# {exercise_id}: {title}\n\n"
        "> Scaffolded by `aicg org execute-plan`. The exercise prompt lands "
        "here on the next autonomous cycle.\n\n"
        f"{duration}"
        "## Objective\n\n"
        "TBD.\n\n"
        "## Prerequisites\n\n"
        "TBD.\n\n"
        "## Steps\n\n"
        "TBD.\n\n"
        "## Acceptance criteria\n\n"
        "TBD.\n\n"
        "## Stretch goals\n\n"
        "TBD.\n"
    )


def _exercise_solution_placeholder(module_id: str, exercise_id: str, slug: str) -> str:
    name = slug.replace("-", " ").title() if slug else exercise_id
    return (
        f"# {module_id}/{exercise_id} ({name}) — Solution\n\n"
        "> Scaffolded by `aicg org execute-plan`. The reference solution lands "
        "here on the next autonomous cycle.\n"
    )


def _project_learning_placeholder(
    project_id: str, title: str, hours: Any, project: dict[str, Any]
) -> str:
    duration = f"\n**Estimated effort:** {hours} hours\n" if hours else ""
    return (
        f"# {project_id}: {title}\n\n"
        "> Scaffolded by `aicg org execute-plan`. Project specification lands "
        "here on the next autonomous cycle.\n"
        f"{duration}"
        "\n## Outcome\n\n"
        "TBD.\n\n"
        "## Architecture\n\n"
        "TBD.\n\n"
        "## Deliverables\n\n"
        "TBD.\n\n"
        "## Acceptance criteria\n\n"
        "TBD.\n"
    )


def _project_solution_placeholder(project_id: str, title: str) -> str:
    return (
        f"# {project_id}: {title} — Solution\n\n"
        "> Scaffolded by `aicg org execute-plan`. The reference walkthrough "
        "lands here on the next autonomous cycle.\n"
    )
