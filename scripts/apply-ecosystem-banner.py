#!/usr/bin/env python3
"""Apply the unified AI Career Curriculum ecosystem banner across every
learning/solutions repo in all four curriculum orgs, via the GitHub API.

The banner is marker-delimited (``<!-- aicg:site-banner -->`` …
``<!-- /aicg:site-banner -->``) so it's idempotent: re-running replaces the
block in place. Repos with no existing banner get one inserted right after
the H1. Operating through the API avoids cloning ~40 repos across 4 orgs.

Dry-run by default; pass --apply to commit each changed README via the API.

  python scripts/apply-ecosystem-banner.py            # preview
  python scripts/apply-ecosystem-banner.py --apply    # ship
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path

OPEN = "<!-- aicg:site-banner -->"
CLOSE = "<!-- /aicg:site-banner -->"

# Unified ecosystem banner. Establishes the five-org federation and keeps the
# cohort/teams funnel (the only live landing site) reachable from every repo.
BANNER_BODY = (
    "> 🎓 Part of the free, open-source **AI Career Curriculum** ecosystem — "
    "[Infrastructure](https://github.com/ai-infra-curriculum) · "
    "[ML Engineering](https://github.com/ml-engineering-curriculum) · "
    "[AI Engineering](https://github.com/ai-engineering-curriculum) · "
    "[Governance](https://github.com/ai-governance-curriculum) · "
    "[Startup](https://github.com/ai-startup-curriculum). "
    "Live cohorts &amp; team programs: "
    "**[ai-infra-curriculum.github.io](https://ai-infra-curriculum.github.io/)**."
)
BLOCK = f"{OPEN}\n{BANNER_BODY}\n{CLOSE}"

REPO_ROOT = Path(__file__).resolve().parent.parent
DOMAINS_DIR = REPO_ROOT / "config" / "domains"
ORG_MANIFEST = REPO_ROOT / "config" / "aicg-org.yaml"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8"))


def org_repos() -> list[tuple[str, str]]:
    """(org, repo) pairs for every learning/solutions repo in all manifests."""
    pairs: list[tuple[str, str]] = []
    manifests = [ORG_MANIFEST] + sorted(DOMAINS_DIR.glob("*.yaml"))
    for mpath in manifests:
        data = _load(mpath)
        org = data["org"]
        for role in data.get("roles", []):
            pairs.append((org, role["learning_repo"]))
            pairs.append((org, role["solution_repo"]))
    return pairs


def _gh_json(args: list[str]) -> dict | None:
    proc = subprocess.run(
        ["gh", "api", *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout)


def apply_banner(body: str) -> tuple[str, bool]:
    """Return (new_body, changed). Replace an existing block, else insert
    after the first H1 (or prepend)."""
    if OPEN in body and CLOSE in body:
        pre, rest = body.split(OPEN, 1)
        _, post = rest.split(CLOSE, 1)
        # Replace the marker block in place, preserving surrounding text.
        new = pre + BLOCK + post
        return new, new != body
    lines = body.splitlines()
    out: list[str] = []
    inserted = False
    for ln in lines:
        out.append(ln)
        if not inserted and ln.startswith("# "):
            out.append("")
            out.append(BLOCK)
            inserted = True
    if not inserted:
        out = [BLOCK, ""] + lines
    new = "\n".join(out)
    if body.endswith("\n") and not new.endswith("\n"):
        new += "\n"
    return new, new != body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Commit changes via the API.")
    ap.add_argument("--branch", default="main")
    args = ap.parse_args(argv)

    changed = unchanged = missing = 0
    for org, repo in org_repos():
        meta = _gh_json([f"repos/{org}/{repo}/contents/README.md", "--jq", "{sha:.sha,content:.content}"])
        if meta is None:
            print(f"  ?? {org}/{repo}: no README.md")
            missing += 1
            continue
        body = base64.b64decode(meta["content"]).decode("utf-8")
        new_body, did_change = apply_banner(body)
        if not did_change:
            unchanged += 1
            continue
        changed += 1
        if not args.apply:
            print(f"  ~~ {org}/{repo}: would update banner")
            continue
        b64 = base64.b64encode(new_body.encode("utf-8")).decode("ascii")
        res = subprocess.run(
            [
                "gh", "api", "-X", "PUT", f"repos/{org}/{repo}/contents/README.md",
                "-f", "message=docs: unify site banner to AI Career Curriculum ecosystem",
                "-f", f"content={b64}",
                "-f", f"sha={meta['sha']}",
                "-f", f"branch={args.branch}",
            ],
            capture_output=True, text=True, check=False,
        )
        ok = res.returncode == 0
        print(f"  {'✓' if ok else '✗'} {org}/{repo}" + ("" if ok else f": {res.stderr[-200:]}"))

    print(f"\n{'APPLIED' if args.apply else 'DRY-RUN'}: {changed} changed, {unchanged} already-current, {missing} missing README")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
