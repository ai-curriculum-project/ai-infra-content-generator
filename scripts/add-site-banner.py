#!/usr/bin/env python3
"""Insert (or refresh) the curriculum-site banner at the top of each public
repo's root README.md.

Marker-delimited (``<!-- aicg:site-banner -->`` … ``<!-- /aicg:site-banner -->``)
so it's idempotent and update-safe: re-running replaces the block in place, so
changing BANNER_BODY below and re-running updates every repo. Mirrors the
``aicg:maintained-by`` footer convention.

Dry-run by default; pass --apply to write + commit + push per repo.

  python scripts/add-site-banner.py --workspace /path/to/ws            # preview
  python scripts/add-site-banner.py --workspace /path/to/ws --apply    # ship
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SITE = "https://ai-curriculum-project.github.io/"
OPEN = "<!-- aicg:site-banner -->"
CLOSE = "<!-- /aicg:site-banner -->"

# NOTE: superseded by scripts/apply-ecosystem-banner.py, which applies the
# unified banner across ALL FOUR curriculum orgs via the GitHub API. This
# single-org local-clone script is kept for offline use; BANNER_BODY mirrors
# the unified ecosystem banner so the two converge.
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from aicg.domain_provision import ecosystem_banner_body  # noqa: E402

BANNER_BODY = ecosystem_banner_body()
BLOCK = f"{OPEN}\n{BANNER_BODY}\n{CLOSE}"

# Public repos to touch (local dir names). The site repo itself is excluded.
LEARNING_SOLUTIONS_ROLES = [
    "junior-engineer", "engineer", "senior-engineer", "principal-engineer",
    "architect", "senior-architect", "principal-architect", "team-lead",
    "chief-ai-officer", "mlops", "ml-platform", "performance", "security",
    "agentic-ai-developer", "agentic-ai-engineer", "senior-agentic-ai-engineer",
    "agentic-systems-architect",
]
EXTRA_REPOS = ["ai-infra-content-generator", "ai-agent-guidebook", ".github"]


def target_repos() -> list[str]:
    repos: list[str] = []
    for role in LEARNING_SOLUTIONS_ROLES:
        repos.append(f"ai-infra-{role}-learning")
        repos.append(f"ai-infra-{role}-solutions")
    repos.extend(EXTRA_REPOS)
    return repos


def strip_block(lines: list[str]) -> list[str]:
    """Remove an existing banner block (markers inclusive) + a trailing blank."""
    if OPEN not in "\n".join(lines):
        return lines
    out: list[str] = []
    skipping = False
    for ln in lines:
        if ln.strip() == OPEN:
            skipping = True
            continue
        if skipping:
            if ln.strip() == CLOSE:
                skipping = False
            continue
        out.append(ln)
    # collapse a double blank left where the block was
    cleaned: list[str] = []
    for ln in out:
        if ln.strip() == "" and cleaned and cleaned[-1].strip() == "":
            continue
        cleaned.append(ln)
    return cleaned


def insert_banner(text: str) -> str:
    lines = text.splitlines()
    lines = strip_block(lines)
    # find first H1
    h1 = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), None)
    block_lines = ["", BLOCK, ""]
    if h1 is None:
        new = block_lines + lines
    else:
        new = lines[: h1 + 1] + block_lines + lines[h1 + 1 :]
    result = "\n".join(new)
    if not result.endswith("\n"):
        result += "\n"
    # avoid 3+ consecutive blanks
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    ws = args.workspace.expanduser().resolve()

    changed = skipped = missing = dirty = 0
    for name in target_repos():
        repo = ws / name
        readme = repo / "README.md"
        if not (repo / ".git").exists() or not readme.exists():
            print(f"  -- missing: {name}")
            missing += 1
            continue
        original = readme.read_text(encoding="utf-8")
        updated = insert_banner(original)
        if updated == original:
            print(f"  ok already current: {name}")
            skipped += 1
            continue
        if not args.apply:
            print(f"  WOULD update: {name}")
            changed += 1
            continue
        if git(repo, "status", "--porcelain").stdout.strip():
            print(f"  !! dirty, skipping: {name}")
            dirty += 1
            continue
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch not in ("main", "master"):
            print(f"  !! not on main ({branch}), skipping: {name}")
            dirty += 1
            continue
        readme.write_text(updated, encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-q", "-m",
            "docs: update AI Infrastructure Curriculum site banner in README")
        push = git(repo, "push", "origin", branch)
        ok = "OK" if push.returncode == 0 else f"PUSH FAILED: {push.stderr.strip()[:80]}"
        print(f"  updated + pushed: {name} [{ok}]")
        changed += 1

    print(f"\n{'APPLIED' if args.apply else 'DRY-RUN'}: "
          f"{changed} changed, {skipped} current, {missing} missing, {dirty} skipped(dirty/branch)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
