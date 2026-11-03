"""Keep generated org profile pages in step with config.

The renderer (``org_profile.render_rich_profile``) is the only thing that
writes ``<org>/.github/profile/README.md``. The weekly ``publish`` job calls
this, then commits whatever changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .org_profile import ProfileError, load_profile, render_rich_profile


def publish_profiles(family: dict[str, Any], profiles_dir: Path, workspace: Path) -> list[Path]:
    """Render each domain's profile into its ``.github`` checkout; return files that changed."""
    written: list[Path] = []
    for domain, manifest in family.items():
        profile_path = Path(profiles_dir) / f"{domain}.json"
        checkout = manifest.repo_path(workspace, ".github")
        if not profile_path.exists() or not checkout.is_dir():
            continue
        try:
            text = render_rich_profile(manifest, load_profile(profile_path))
        except ProfileError:
            continue
        target = checkout / "profile" / "README.md"
        if target.exists() and target.read_text(encoding="utf-8") == text:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written
