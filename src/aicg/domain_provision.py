"""One-command domain provisioning (roadmap §3, `bootstrap-domain`).

Wraps the now-domain-aware ``bootstrap_role`` with the org-level scaffolding
that previously had to be done by hand when standing up a sibling org:
generate the ``.github`` org-profile README (role-ladder table + the
four-org "AI Career Curriculum ecosystem" cross-links), then scaffold every
role's paired repos.

The GitHub *org* itself still has to exist (creating an org needs
``admin:org`` / the web UI, outside the runner's token scope) — this targets
an existing, empty org. The profile renderer is pure so it's testable; the
CLI layer does the repo creation + pushes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The four-org federation, in value-chain order. Used to render the
# "Curriculum Family" cross-link block in every org profile.
ECOSYSTEM = [
    ("ai-infra-curriculum", "AI Infrastructure", "*run* the platforms (Kubernetes, GPUs, training infra, serving, MLOps, IaC, SRE)"),
    ("ml-engineering-curriculum", "ML Engineering", "*build & train* the models (data, fine-tuning, pretraining, RLHF, evals)"),
    ("ai-engineering-curriculum", "AI Engineering", "*build with* the models (agentic apps, RAG, multi-agent systems)"),
    ("ai-governance-curriculum", "AI Governance", "*govern & assure* AI (security, compliance, evaluation, safety)"),
    ("ai-startup-curriculum", "AI Startup", "*turn it into a company* — the startup operating school for technical founders (functional curricula × role pathways × stages)"),
]


@dataclass(frozen=True)
class _RoleRow:
    title: str
    level: int
    learning_repo: str
    solution_repo: str | None


def _org_display(org: str) -> str:
    base = org[: -len("-curriculum")] if org.lower().endswith("-curriculum") else org
    special = {
        "ai-infra": "AI Infrastructure",
        "ml-engineering": "ML Engineering",
        "ai-engineering": "AI Engineering",
        "ai-governance": "AI Governance",
        "ai-startup": "AI Startup",
    }
    return special.get(base.lower(), base.replace("-", " ").title())


def render_org_profile(manifest: Any, *, tagline: str | None = None) -> str:
    """Render the ``.github/profile/README.md`` for a domain's org. Pure."""
    org = manifest.org
    display = _org_display(org)
    rows = [
        _RoleRow(r.title, r.level, r.learning_repo, r.solution_repo)
        for r in sorted(manifest.roles, key=lambda r: r.level)
    ]
    tag = tagline or f"A hands-on, role-based curriculum for the **{display}** career ladder."

    out: list[str] = []
    out.append(f"# {display} Curriculum\n")
    out.append(f"> {tag}\n")
    out.append(
        "[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)]"
        "(https://opensource.org/licenses/MIT)\n"
    )
    out.append("## ⚠️ AI-Generated Content Disclaimer\n")
    out.append(
        "> The content in these repositories is generated with AI assistance and "
        "undergoes ongoing human review. It may contain errors or outdated "
        "information. Treat it as a learning resource: cross-reference official "
        "docs, test code in a safe environment, and report issues via GitHub "
        "Issues or Discussions.\n"
    )
    out.append("## 📚 Role Ladder\n")
    out.append("| Role | Level | Repositories |")
    out.append("|------|-------|--------------|")
    for r in rows:
        learn = f"https://github.com/{org}/{r.learning_repo}"
        if r.solution_repo:
            sol = f"https://github.com/{org}/{r.solution_repo}"
            repos = f"[📘 Learning]({learn}) · [✅ Solutions]({sol})"
        else:
            # Single-repo curriculum: one repo, exemplars live in-repo.
            repos = f"[📗 Curriculum]({learn})"
        out.append(f"| **{r.title}** | L{r.level} | {repos} |")
    out.append("")
    out.append("## 📈 Career Progression\n")
    out.append(
        "See the **[Career Progression Guide](./CAREER_PROGRESSION.md)** for the "
        "full ladder — level descriptions, skills matrix, compensation ranges, "
        "promotion criteria, and specialist tracks.\n"
    )
    out.append("## 🔗 Curriculum Family\n")
    out.append(
        f"One of {len(ECOSYSTEM)} sibling orgs in the **AI Career Curriculum "
        "ecosystem**, organized by what you *do* relative to a model:\n"
    )
    for sib_org, sib_disp, sib_desc in ECOSYSTEM:
        if sib_org == org:
            out.append(f"- **{sib_disp} Curriculum** *(you are here)* — {sib_desc}.")
        else:
            out.append(f"- **[{sib_disp} Curriculum](https://github.com/{sib_org})** — {sib_desc}.")
    out.append("")
    out.append("---\n")
    out.append("<!-- aicg:maintained-by -->")
    mb = getattr(manifest, "maintained_by", None) or {}
    phrasing = mb.get("phrasing") or (
        f"Maintained by [{mb.get('name')}]({mb.get('url')})"
        if mb.get("name") and mb.get("url")
        else "Maintained by the curriculum maintainers"
    )
    out.append(phrasing)
    return "\n".join(out) + "\n"
