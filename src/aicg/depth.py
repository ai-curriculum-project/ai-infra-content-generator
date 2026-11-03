"""Depth checks: is this real content, or scaffolding?

The structural audits ask whether a file *exists*. Scaffolding creates every
file up front (a ~30-word "the reference solution lands here" README per
exercise, a ~700-word outline per module), so existence says nothing about
whether the content was written. These checks measure substance instead and
are shared by the audits (to queue the work) and the completeness matrix (to
report it).

Thresholds were calibrated on the live repos (2026-11-03): scaffolded
exercise solutions are 20-30 words, real ones 400+ (code-heavy ones count
their .tf/.cu/.py files); skeleton modules are 675-1,300 words, authored
modules 10,000+.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MIN_MODULE_WORDS = 3000
MIN_EXERCISE_SOLUTION_WORDS = 60
SCAFFOLD_PHRASES = (
    "lands here on the next autonomous cycle",
    "walkthroughs land here",
    "scaffolded by `aicg",
    "arrive over subsequent autonomous cycles",
    "scaffolded placeholder",
)
_SKIP_DIRS = {".git", ".aicg", "node_modules", ".venv", "__pycache__"}
_NON_CHAPTER = {"readme.md", "quiz.md", "resources.md"}


def _is_text(path: Path) -> bool:
    try:
        return b"\x00" not in path.read_bytes()[:2048]
    except OSError:
        return False


def _files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    return [
        p for p in root.rglob("*")
        if p.is_file() and not (_SKIP_DIRS & set(p.relative_to(root).parts))
    ]


def text_words(root: Path) -> int:
    """Whitespace-separated words across every text file under ``root``."""
    total = 0
    for path in _files(root):
        if _is_text(path):
            total += len(path.read_text(encoding="utf-8", errors="replace").split())
    return total


def _has_scaffold_phrase(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(phrase in text for phrase in SCAFFOLD_PHRASES)


def exercise_solution_is_stub(path: Path) -> bool:
    """True when an exercise's solution (a dir or a single file) has no real content.

    A stub is either tiny overall, or a scaffold README with nothing of
    substance beside it.
    """
    files = [p for p in _files(path) if _is_text(p)]
    if not files:
        return True
    if text_words(path) < MIN_EXERCISE_SOLUTION_WORDS:
        return True
    scaffolds = [p for p in files if _has_scaffold_phrase(p)]
    if not scaffolds:
        return False
    rest = sum(len(p.read_text(encoding="utf-8", errors="replace").split())
               for p in files if p not in scaffolds)
    return rest < MIN_EXERCISE_SOLUTION_WORDS


@dataclass(frozen=True)
class ModuleDepth:
    words: int
    chapters: int
    exercises: int
    has_quiz: bool

    @property
    def status(self) -> str:
        if self.words == 0:
            return "empty"
        return "complete" if self.words >= MIN_MODULE_WORDS else "thin"


def _chapters(module: Path) -> int:
    root = [p for p in module.glob("*.md") if p.name.lower() not in _NON_CHAPTER]
    notes = list((module / "lecture-notes").glob("*.md"))
    return len(root) + len(notes)


def _exercises(module: Path) -> int:
    files = list((module / "exercises").glob("exercise-*.md"))
    dirs = [p for p in module.glob("exercise-*") if p.is_dir()]
    return len(files) + len(dirs)


def learning_module_depth(module: Path) -> ModuleDepth:
    return ModuleDepth(
        words=text_words(module),
        chapters=_chapters(module),
        exercises=_exercises(module),
        has_quiz=(module / "quiz.md").exists(),
    )
