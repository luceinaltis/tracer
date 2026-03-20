"""Validate skills/ files against docs/contrib/skills-rules.md."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
ERRORS: list[str] = []


def error(skill: str, msg: str) -> None:
    ERRORS.append(f"skills/{skill}: {msg}")


def parse_frontmatter(content: str) -> dict[str, str]:
    """Extract YAML frontmatter as a simple key-value dict."""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fm: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            # handle multi-line description with '>'
            if value == ">":
                continue
            if key and value:
                fm[key] = value
            elif key and not value:
                # might be a continuation line from '>' — check next
                fm.setdefault(key, "")
    return fm


def check_skill_md_exists(skill_dir: Path) -> bool:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        error(skill_dir.name, "missing SKILL.md")
        return False
    return True


def check_frontmatter(skill_dir: Path, content: str) -> None:
    name = skill_dir.name
    fm = parse_frontmatter(content)

    if "name" not in fm:
        error(name, "frontmatter missing 'name' field")
    elif fm["name"] != name:
        error(name, f"frontmatter name '{fm['name']}' does not match directory '{name}'")

    if "description" not in fm:
        # check for multi-line description with '>'
        if "description:" not in content.split("---")[1] if "---" in content else "":
            error(name, "frontmatter missing 'description' field")


def check_title(skill_dir: Path, content: str) -> None:
    # strip frontmatter
    body = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)
    lines = body.strip().splitlines()
    if not lines or not lines[0].startswith("# "):
        error(skill_dir.name, "missing top-level '# Title' heading")


def check_required_sections(skill_dir: Path, content: str) -> None:
    body = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)
    headings = re.findall(r"^## (.+)", body, re.MULTILINE)

    has_input = any(h.lower().startswith(("input", "before you start")) for h in headings)
    if not has_input:
        error(skill_dir.name, "missing '## Input' or '## Before You Start' section")

    # must have at least one other section beyond input
    if len(headings) < 2:
        error(skill_dir.name, "must have at least one workflow section beyond input/prerequisites")


def check_empty_sections(skill_dir: Path, content: str) -> None:
    body = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)
    sections = re.split(r"^(## .+)", body, flags=re.MULTILINE)

    i = 1
    while i < len(sections) - 1:
        heading = sections[i].strip()
        body_text = sections[i + 1].strip()
        if not body_text:
            error(skill_dir.name, f"empty section: '{heading}'")
        i += 2


def check_dirname(skill_dir: Path) -> None:
    name = skill_dir.name
    if name != name.lower():
        error(name, "directory name must be lowercase")
    if "_" in name:
        error(name, "use hyphens, not underscores in directory name")


def validate_skills() -> None:
    if not SKILLS_DIR.exists():
        print("No skills/ directory found.")
        return

    skill_dirs = [d for d in SKILLS_DIR.iterdir() if d.is_dir()]
    if not skill_dirs:
        print("No skills found.")
        return

    for skill_dir in skill_dirs:
        check_dirname(skill_dir)

        if not check_skill_md_exists(skill_dir):
            continue

        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        check_frontmatter(skill_dir, content)
        check_title(skill_dir, content)
        check_required_sections(skill_dir, content)
        check_empty_sections(skill_dir, content)


if __name__ == "__main__":
    validate_skills()

    if ERRORS:
        print(f"Found {len(ERRORS)} error(s):\n")
        for e in ERRORS:
            print(f"  FAIL: {e}")
        sys.exit(1)
    else:
        print("All skills passed validation.")
