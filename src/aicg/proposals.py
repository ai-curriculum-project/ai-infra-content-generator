"""Shared pieces of proposal issues: where they live and their hidden data block."""

from __future__ import annotations

import json
import re
from typing import Any

PARENT_REPO = "ai-curriculum-project/.github"
_META_RE = re.compile(r"<!-- aicg:proposal (\{.*?\}) -->", re.S)


def proposal_meta(kind: str, **fields: Any) -> str:
    """A hidden HTML comment the approvals job reads back; invisible on GitHub."""
    return f"<!-- aicg:proposal {json.dumps({'kind': kind, **fields}, sort_keys=True)} -->"


def parse_meta(body: str) -> dict[str, Any] | None:
    match = _META_RE.search(body or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
