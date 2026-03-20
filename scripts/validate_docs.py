"""Validate docs/ files against docs/contrib/docs-rules.md."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "docs"
ERRORS: list[str] = []


def error(file: Path, msg: str) -> None:
    rel = file.relative_to(ROOT)
    ERRORS.append(f"{rel}: {msg}")


def check_filename(file: Path) -> None:
    if file.name != file.name.lower():
        error(file, "filename must be lowercase")
    if "_" in file.stem:
        error(file, "use hyphens, not underscores in filename")


def check_title(file: Path, lines: list[str]) -> None:
    if not lines or not lines[0].startswith("# "):
        error(file, "must start with a top-level '# Title' heading")


def check_sections(file: Path, content: str) -> None:
    if not re.search(r"^## ", content, re.MULTILINE):
        error(file, "must have at least one '## Section' heading")


def check_code_blocks(file: Path, content: str) -> None:
    fences = re.findall(r"^```(\w*)", content, re.MULTILINE)
    for i, lang in enumerate(fences):
        # opening fences (even index) must have a language tag
        if i % 2 == 0 and not lang:
            error(file, "code block without language tag (use ```python, ```bash, etc.)")
            break


def check_internal_links(file: Path, content: str) -> None:
    links = re.findall(r"\[.*?\]\(([^)]+)\)", content)
    for link in links:
        if link.startswith("http") or link.startswith("#"):
            continue
        target = (file.parent / link.split("#")[0]).resolve()
        if not target.exists():
            error(file, f"broken internal link: {link}")


def validate_docs() -> None:
    md_files = list(DOCS_DIR.rglob("*.md"))
    if not md_files:
        print("No docs found.")
        return

    for file in md_files:
        content = file.read_text(encoding="utf-8")
        lines = content.strip().splitlines()

        check_filename(file)
        check_title(file, lines)
        check_sections(file, content)
        check_code_blocks(file, content)
        check_internal_links(file, content)


if __name__ == "__main__":
    validate_docs()

    if ERRORS:
        print(f"Found {len(ERRORS)} error(s):\n")
        for e in ERRORS:
            print(f"  FAIL: {e}")
        sys.exit(1)
    else:
        print("All docs passed validation.")
