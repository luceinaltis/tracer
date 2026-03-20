# Skills Rules

Validation rules for files under `skills/`.

## Structure

Every skill must be a directory containing at least `SKILL.md`:

```text
skills/
└── {skill-name}/
    └── SKILL.md
```

## SKILL.md Required Fields

Every `SKILL.md` must have YAML frontmatter with:

```yaml
---
name: {skill-name}        # must match directory name
description: >             # one-line summary of when to use this skill
  ...
---
```

Both `name` and `description` are mandatory. Missing either is a validation failure.

## SKILL.md Required Sections

The Markdown body must include at minimum:

| Section | Purpose |
|---------|---------|
| `# {Skill Name}` | Top-level heading matching the skill name |
| `## Input` or `## Before You Start` | What the skill needs before execution |
| At least one workflow section | The actual steps or instructions |

## Content Rules

- All content must be in English.
- No placeholder sections (e.g., `## TODO` with no content).
- If the skill references data providers or capabilities, they must match those defined in `docs/architecture.md`.
- If the skill references agent roles, they must match those defined in `docs/conversational-layer.md`.

## Naming

- Directory names: lowercase, hyphen-separated (e.g., `analyze-market`).
- `name` in frontmatter must exactly match the directory name.

## Consistency Checks

- Skills must not duplicate logic already defined in another skill. If two skills share a workflow step, one should invoke the other (e.g., `analyze-market` Step 7 invokes `/alpha-report`).
- Output format templates in skills must align with the response format defined in `docs/conversational-layer.md`.

## Validation Checklist

- [ ] Directory contains `SKILL.md`
- [ ] Frontmatter has `name` and `description`
- [ ] `name` matches directory name
- [ ] Top-level heading present
- [ ] At least one input/prerequisite section
- [ ] At least one workflow section
- [ ] No empty placeholder sections
- [ ] All cross-references to roles/providers are valid
