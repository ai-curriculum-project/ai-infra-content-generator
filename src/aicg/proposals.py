"""Proposal issues: where they live, the runner's record of them, and what's valid.

Issue bodies are untrusted: anyone can open an issue in a public repo, and an
author can edit the body after a maintainer approves it. So nothing the
approvals job acts on comes from the body. When the runner opens a proposal
issue it records the proposal here, keyed by issue number, and the approvals
job reads only that record (and also requires the issue to be authored by the
runner's own account).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PARENT_REPO = "ai-curriculum-project/.github"
ROLE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ITEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_TITLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &/,.'()+-]{1,79}$")


def store_dir(runner_root: Path) -> Path:
    return Path(runner_root) / ".aicg" / "org" / "proposals"


def issue_number(url: str) -> int | None:
    match = re.search(r"/issues/(\d+)\s*$", url or "")
    return int(match.group(1)) if match else None


def record(store: Path, number: int, meta: dict[str, Any]) -> None:
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{number}.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n",
                                          encoding="utf-8")


def load(store: Path, number: int) -> dict[str, Any] | None:
    try:
        data = json.loads((store / f"{int(number)}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clean(text: Any, limit: int = 2000) -> str:
    """Untrusted text (agent output) made safe to render in an issue body."""
    out = str(text or "").replace("<!--", "").replace("-->", "")
    return out.replace("\r", "")[:limit]


def _valid_delta(delta: Any) -> bool:
    if delta is None:
        return True
    if not isinstance(delta, dict):
        return False
    for kind in ("modules", "exercises", "projects"):
        items = delta.get(kind, [])
        if not isinstance(items, list) or len(items) > 10:
            return False
        for item in items:
            if not isinstance(item, dict):
                return False
            ident = item.get("id") or item.get("slug")
            if ident is not None and not ITEM_ID_RE.match(str(ident)):
                return False
    return True


def validate(meta: Any, *, orgs: dict[str, str], roles: dict[str, set[str]],
             learning_repo_for: dict[tuple[str, str], str]) -> str | None:
    """Return why ``meta`` must not be acted on, or None when it's safe."""
    if not isinstance(meta, dict):
        return "no runner record for this issue"
    domain, role_id, kind = meta.get("domain"), meta.get("role_id"), meta.get("kind")
    if domain not in orgs:
        return f"unknown domain {domain!r}"
    if not isinstance(role_id, str) or not ROLE_ID_RE.match(role_id):
        return f"invalid role id {role_id!r}"
    if kind == "content":
        if role_id not in roles.get(domain, set()):
            return f"role {role_id!r} is not in {domain}"
        learning = learning_repo_for.get((domain, role_id), "")
        expected = rf"^https://github\.com/{re.escape(orgs[domain])}/{re.escape(learning)}/pull/\d+$"
        if not re.match(expected, str(meta.get("pr_url", "")), re.IGNORECASE):
            return "proposal PR is not in this role's learning repo"
        if not MONTH_RE.match(str(meta.get("month", ""))):
            return "invalid month"
        if not _valid_delta(meta.get("delta")):
            return "plan change has an unexpected shape"
        return None
    if kind == "role":
        if any(role_id in ids for ids in roles.values()):
            return f"role {role_id!r} already exists"
        if not _TITLE_RE.match(str(meta.get("title", ""))):
            return "invalid title"
        level = meta.get("level")
        if not isinstance(level, int) or not 10 <= level <= 90:
            return "invalid level"
        aliases = meta.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) and _TITLE_RE.match(a) for a in aliases):
            return "invalid aliases"
        return None
    return f"unknown proposal kind {kind!r}"
