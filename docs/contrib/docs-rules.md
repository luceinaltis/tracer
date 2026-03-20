# Docs Rules

Validation rules for files under `docs/`.

## Format

- All docs must be written in English.
- Use Markdown with a single `# Title` at the top.
- No trailing whitespace. End file with a single newline.

## Required Structure

Every doc file must have:
- `# Title` — top-level heading matching the file's purpose.
- At least one `##` section.

## Links

- All internal links must point to existing files. Example: `[see architecture](../architecture.md)`
- No absolute paths to local filesystem. Use relative paths only.
- References to AGENTS.md sections must match actual section headings.

## Consistency with AGENTS.md

- If a doc references a project structure directory (e.g., `src/tracer/conversation/`), it must exist in the AGENTS.md Project Structure tree.
- If a doc defines LLM roles or data capabilities, they must match the definitions in AGENTS.md or the authoritative doc (`conversational-layer.md` for roles, `architecture.md` for capabilities).
- Tech stack versions (Python, tools) must not contradict AGENTS.md.

## Content

- No placeholder or TODO-only sections. Every section must have substantive content.
- Code examples must specify a language tag (e.g., ` ```python `).
- Diagrams (ASCII art) must render correctly in a monospace font — no single-line compressed trees.

## Naming

- Filenames: lowercase, hyphen-separated (e.g., `memory-system.md`).
- No spaces or underscores in filenames.
