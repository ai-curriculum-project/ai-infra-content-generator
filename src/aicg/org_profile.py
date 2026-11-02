"""Rich, profile-driven org README renderer.

``domain_provision.render_org_profile`` writes a minimal org page (role table +
ecosystem links) when an org is first bootstrapped. The ai-infra org page grew
into something much fuller by hand: an overview, roles grouped by career level
with a focus column, quick start, technologies, contributing, license, and
support. This module renders that same layout for any domain from a small
JSON profile in ``config/profiles/<domain>.json``, so every org page can match
it and stay consistent.

Facts that live in the manifest (roles, levels, repos, counts) are read from
the manifest; only editorial content (tagline, overview, focus lines,
technologies) lives in the profile. Pure: no I/O besides ``load_profile``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain_provision import ECOSYSTEM, _org_display

PROFILE_KEYS = {
    "tagline",
    "overview",
    "highlights",
    "sections",
    "bands",
    "tracks_heading",
    "track_note",
    "focus",
    "quick_start_role",
    "technologies_heading",
    "technologies",
    "discussions",
    "career_progression",
    "role_column",
}
REQUIRED_KEYS = {"tagline", "overview", "focus", "quick_start_role"}
DISCLAIMER = (
    "> The content in these repositories is generated with AI assistance and "
    "undergoes ongoing human review. It may contain errors or outdated "
    "information. Treat it as a learning resource: cross-reference official docs, "
    "test code in a safe environment, and report issues via GitHub Issues or "
    "Discussions."
)


class ProfileError(ValueError):
    """Raised when a profile is malformed or doesn't match the manifest."""


def load_profile(path: Path) -> dict[str, Any]:
    """Load and shape-check a profile JSON file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    unknown = sorted(set(data) - PROFILE_KEYS)
    if unknown:
        raise ProfileError(f"{path}: unknown key(s) {unknown}; allowed: {sorted(PROFILE_KEYS)}")
    missing = sorted(REQUIRED_KEYS - set(data))
    if missing:
        raise ProfileError(f"{path}: missing required key(s) {missing}")
    return data


# --- helpers -----------------------------------------------------------------------


def _roles(manifest: Any) -> list[Any]:
    return sorted(manifest.roles, key=lambda r: (r.level, r.title))


def _repo_links(org: str, role: Any) -> str:
    learn = f"https://github.com/{org}/{role.learning_repo}"
    if role.is_single or not role.solution_repo:
        return f"[📗 Curriculum]({learn})"
    return f"[📘 Learning]({learn}) · [✅ Solutions](https://github.com/{org}/{role.solution_repo})"


def _repo_counts(manifest: Any) -> tuple[int, int, int]:
    """(total repos, learning repos, solution repos) owned by roles."""
    learning = len(manifest.roles)
    solutions = sum(1 for r in manifest.roles if r.solution_repo and not r.is_single)
    return learning + solutions, learning, solutions


def _in_band(band: dict[str, Any], role: Any) -> bool:
    """A band either lists role ids explicitly or covers a level range."""
    if "roles" in band:
        return role.id in band["roles"]
    return band["min_level"] <= role.level <= band["max_level"]


def _validate(manifest: Any, profile: dict[str, Any]) -> None:
    ids = {r.id for r in manifest.roles}
    missing = sorted(ids - set(profile["focus"]))
    if missing:
        raise ProfileError(f"profile has no focus line for role(s): {missing}")
    if profile["quick_start_role"] not in ids:
        raise ProfileError(f"quick_start_role {profile['quick_start_role']!r} is not a role")
    for role in manifest.roles:
        bands = profile.get("bands")
        if bands and not any(_in_band(b, role) for b in bands):
            raise ProfileError(f"role {role.id!r} (L{role.level}) is outside every band")


# --- sections ----------------------------------------------------------------------


def _header(manifest: Any, profile: dict[str, Any]) -> list[str]:
    org, display = manifest.org.lower(), _org_display(manifest.org)
    total, _, _ = _repo_counts(manifest)
    contrib_url = (
        f"https://github.com/orgs/{org}/discussions"
        if profile.get("discussions")
        else f"https://github.com/orgs/{org}/repositories"
    )
    return [
        f"# {display} Curriculum\n",
        "## ⚠️ AI-Generated Content Disclaimer\n",
        DISCLAIMER + "\n",
        "---\n",
        f"> {profile['tagline']}\n",
        "[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)]"
        "(https://opensource.org/licenses/MIT)",
        "[![Contributions Welcome](https://img.shields.io/badge/contributions-welcome-brightgreen.svg)]"
        f"({contrib_url})",
        f"[![Repositories: {total}](https://img.shields.io/badge/repositories-{total}-blue.svg)]"
        f"(https://github.com/orgs/{org}/repositories)\n",
    ]


def _overview(manifest: Any, profile: dict[str, Any]) -> list[str]:
    total, learning, solutions = _repo_counts(manifest)
    tracks = len(manifest.roles)
    if solutions:
        repos = f"**{total} curriculum repositories** ({learning} learning + {solutions} solutions)"
    else:
        noun = "repository" if total == 1 else "repositories"
        repos = f"**{total} curriculum {noun}** (one self-contained repo per curriculum)"
    bullets = [f"- 🏢 {repos}", f"- 📚 **{tracks} role tracks**"]
    bullets += [f"- {h}" for h in profile.get("highlights", [])]
    return ["## 🎯 Overview\n", profile["overview"] + "\n", "**At a glance:**\n", *bullets, ""]


def _custom_sections(profile: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for section in profile.get("sections", []):
        out += [f"## {section['heading']}\n", section["body"].rstrip() + "\n"]
    return out


def _family(manifest: Any) -> list[str]:
    out = [
        "## 🔗 Curriculum Family\n",
        f"One of {len(ECOSYSTEM)} sibling orgs in the **AI Career Curriculum ecosystem**, "
        "organized by what you *do* relative to a model:\n",
    ]
    for sib_org, sib_display, sib_desc in ECOSYSTEM:
        if sib_org.lower() == manifest.org.lower():  # GitHub org names are case-insensitive
            out.append(f"- **{sib_display} Curriculum** *(you are here)* — {sib_desc}.")
        else:
            out.append(
                f"- **[{sib_display} Curriculum](https://github.com/{sib_org})** — {sib_desc}."
            )
    return out + ["", "---\n"]


def _track_table(org: str, roles: list[Any], focus: dict[str, str], label: str) -> list[str]:
    rows = [f"| {label} | Focus | Repositories |", "|------|-------|--------------|"]
    rows += [f"| **{r.title}** | {focus[r.id]} | {_repo_links(org, r)} |" for r in roles]
    return rows + [""]


def _tracks(manifest: Any, profile: dict[str, Any]) -> list[str]:
    org, roles = manifest.org.lower(), _roles(manifest)
    label = profile.get("role_column", "Role")
    out = [f"## {profile.get('tracks_heading', '📚 Learning Tracks')}\n"]
    bands = profile.get("bands")
    if not bands:
        out += _track_table(org, roles, profile["focus"], label)
    for band in bands or []:
        members = [r for r in roles if _in_band(band, r)]
        if members:
            out += [f"### {band['title']}\n", *_track_table(org, members, profile["focus"], label)]
    if profile.get("track_note"):
        out.append(f"> {profile['track_note']}\n")
    return out + ["---\n"]


def _quick_start(manifest: Any, profile: dict[str, Any]) -> list[str]:
    role = next(r for r in manifest.roles if r.id == profile["quick_start_role"])
    repo = role.learning_repo
    single = role.is_single or not role.solution_repo
    steps = [
        "## 🚀 Quick Start\n",
        "1. **Choose a track** from the table above, based on your experience and direction.",
        "2. **Clone its repository:**\n",
        "   ```bash",
        f"   git clone https://github.com/{manifest.org}/{repo}.git",
        f"   cd {repo}",
        "   ```\n",
        "3. **Start with the first module** and read its `README.md`.",
        "4. **Work through the exercises** in each module.",
    ]
    if single:
        steps.append("5. **Use the in-repo exemplars** to check your artifacts.\n")
    else:
        steps.append("5. **Check the paired solutions repository** for reference walkthroughs.\n")
    return steps + ["---\n"]


def _technologies(profile: dict[str, Any]) -> list[str]:
    tech = profile.get("technologies")
    if not tech:
        return []
    heading = profile.get("technologies_heading", "🛠️ Technologies Covered")
    lines = [f"- **{category}:** {items}" for category, items in tech.items()]
    return [f"## {heading}\n", *lines, "", "---\n"]


def _closing(manifest: Any, profile: dict[str, Any]) -> list[str]:
    org = manifest.org.lower()
    discussions = f"https://github.com/orgs/{org}/discussions"
    career = f"https://github.com/{org}/.github/blob/main/CAREER_PROGRESSION.md"
    out: list[str] = []
    if profile.get("career_progression"):
        out += [
            "## 📈 Career Progression\n",
            f"See the **[Career Progression Guide]({career})** for level descriptions, a "
            "skills matrix, promotion criteria, and specialist tracks.\n",
            "---\n",
        ]
    ideas = (
        f"- Report issues or ideas via [GitHub Discussions]({discussions})"
        if profile.get("discussions")
        else "- Report issues or ideas via the repository's GitHub Issues"
    )
    out += [
        "## 🤝 Contributing\n",
        "Contributions are welcome across the organization:\n",
        "- Fix broken links, stale references, or inaccurate explanations",
        "- Add depth to modules, exercises, or projects",
        "- Improve validation for runnable exercises",
        ideas,
        "- Follow the `CONTRIBUTING.md` in the specific repository you want to improve\n",
        "---\n",
        "## 📜 License\n",
        "Curriculum repositories are MIT-licensed unless noted otherwise. See the "
        "target repository's `LICENSE` file for authoritative terms.\n",
        "---\n",
        "## 📞 Support\n",
        "- **Issues:** use the relevant repository's GitHub Issues",
    ]
    if profile.get("discussions"):
        out.append(f"- **Discussions:** [organization discussions]({discussions})")
    if profile.get("career_progression"):
        out.append(f"- **Docs:** [Career Progression]({career})")
    return out + ["", "---\n"]


def _footer(manifest: Any) -> list[str]:
    mb = getattr(manifest, "maintained_by", None) or {}
    phrasing = mb.get("phrasing") or (
        f"Maintained by [{mb.get('name')}]({mb.get('url')})"
        if mb.get("name") and mb.get("url")
        else "Maintained by the curriculum maintainers"
    )
    return ["<!-- aicg:maintained-by -->", phrasing]


def render_rich_profile(manifest: Any, profile: dict[str, Any]) -> str:
    """Render the full org README for ``manifest`` from ``profile``. Pure."""
    _validate(manifest, profile)
    parts = (
        _header(manifest, profile)
        + _overview(manifest, profile)
        + _custom_sections(profile)
        + _family(manifest)
        + _tracks(manifest, profile)
        + _quick_start(manifest, profile)
        + _technologies(profile)
        + _closing(manifest, profile)
        + _footer(manifest)
    )
    return "\n".join(parts) + "\n"
