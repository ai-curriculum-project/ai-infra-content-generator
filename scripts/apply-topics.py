#!/usr/bin/env python3
"""Set GitHub repo topics on every curriculum repo across all four orgs.

Topics drive discovery (GitHub topic pages + search). Each repo gets a clean,
canonical set: shared curriculum topics + the domain's discipline topics + a
role-derived topic + a learning/solutions marker. Driven by the manifests, so
it stays in lockstep with the role set.

Replaces the full topic list per repo via the API (idempotent). Dry-run by
default; --apply to write.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOMAINS_DIR = REPO_ROOT / "config" / "domains"
ORG_MANIFEST = REPO_ROOT / "config" / "aicg-org.yaml"

SHARED = ["curriculum", "education", "ai", "learning-resources", "career-development"]

DOMAIN_TOPICS = {
    "AI-Infra-Curriculum": ["ai-infrastructure", "mlops", "kubernetes", "machine-learning", "devops", "sre"],
    "ml-engineering-curriculum": ["machine-learning", "ml-engineering", "deep-learning", "model-training", "fine-tuning", "llm"],
    "ai-engineering-curriculum": ["ai-engineering", "llm", "ai-agents", "generative-ai", "rag", "prompt-engineering"],
    "ai-governance-curriculum": ["ai-governance", "responsible-ai", "ai-safety", "ai-security", "ai-risk", "ai-compliance"],
    "ai-startup-curriculum": ["startup", "entrepreneurship", "founders", "fundraising", "venture-capital", "product-management"],
}


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8"))


def _topic(s: str) -> str:
    """Normalize to a valid GitHub topic: lowercase, hyphenated, <=35 chars."""
    t = re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
    return t[:35]


def topics_for(org: str, role_id: str, repo: str) -> list[str]:
    if repo.endswith("-solutions"):
        kind = "solutions"
    elif repo.endswith("-curriculum"):
        kind = "curriculum"
    else:
        kind = "learning"
    role_topic = _topic(role_id)
    out: list[str] = []
    for t in SHARED + DOMAIN_TOPICS.get(org, []) + [role_topic, kind]:
        if t and t not in out:
            out.append(t)
    return out[:20]  # GitHub caps at 20


def org_repos() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for mpath in [ORG_MANIFEST] + sorted(DOMAINS_DIR.glob("*.yaml")):
        data = _load(mpath)
        org = data["org"]
        for role in data.get("roles", []):
            rows.append((org, role["id"], role["learning_repo"]))
            # Single-repo curricula (repo_model: single) have no solution_repo.
            if role.get("solution_repo"):
                rows.append((org, role["id"], role["solution_repo"]))
    return rows


def get_topics(org: str, repo: str) -> list[str] | None:
    proc = subprocess.run(
        ["gh", "api", f"repos/{org}/{repo}/topics", "--jq", ".names"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout)


def set_topics(org: str, repo: str, topics: list[str]) -> bool:
    args = ["gh", "api", "-X", "PUT", f"repos/{org}/{repo}/topics"]
    for t in topics:
        args += ["-f", f"names[]={t}"]
    return subprocess.run(args, capture_output=True, text=True, check=False).returncode == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--org", default=None, help="Only touch repos in this GitHub org (e.g. ai-startup-curriculum).")
    args = ap.parse_args(argv)

    changed = same = missing = 0
    for org, role_id, repo in org_repos():
        if args.org and org != args.org:
            continue
        want = topics_for(org, role_id, repo)
        have = get_topics(org, repo)
        if have is None:
            print(f"  ?? {org}/{repo}: not found")
            missing += 1
            continue
        if sorted(have) == sorted(want):
            same += 1
            continue
        changed += 1
        if not args.apply:
            print(f"  ~~ {org}/{repo}: {len(want)} topics")
            continue
        ok = set_topics(org, repo, want)
        print(f"  {'✓' if ok else '✗'} {org}/{repo}")

    print(f"\n{'APPLIED' if args.apply else 'DRY-RUN'}: {changed} changed, {same} already-set, {missing} missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
