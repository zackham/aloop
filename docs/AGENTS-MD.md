# The AGENTS.md Convention

## What It Is

`AGENTS.md` is a markdown file that provides project-specific instructions to AI coding agents. It tells the agent about your project's architecture, coding conventions, build commands, and anything non-obvious that the agent can't discover by reading code.

The convention is used by multiple tools:
- **Claude Code** loads `CLAUDE.md` into every session
- **Codex CLI** reads `AGENTS.md` for project guidance
- **Gemini CLI** reads `GEMINI.md`
- **aloop** reads `AGENTS.md` (falls back to `CLAUDE.md`)

## How aloop Uses It

aloop checks for project instructions in this order:

1. `AGENTS.md` (project root)
2. `.agents/AGENTS.md`
3. `CLAUDE.md` (project root)
4. `.claude/CLAUDE.md`

The first file found is loaded into the system prompt under `# Project Context` (in section mode).

In template mode, the `{{agents_md}}` variable checks a slightly different order: `ALOOP.md`, `AGENTS.md`, `CLAUDE.md` — this lets projects have a separate operational context file for the aloop harness while keeping `AGENTS.md` as the cross-tool standard.

## What to Put In It

**Include:**
- Build, test, and lint commands that aren't obvious from package files
- Code style rules that differ from language defaults
- Architectural decisions and non-obvious design patterns
- Required environment variables or setup steps
- Testing quirks and gotchas
- Branch naming, PR, and commit conventions
- Important data paths and file locations

**Don't include:**
- File-by-file structure (the agent can discover this)
- Standard language conventions the model already knows
- Generic advice ("write clean code")
- Information that changes frequently
- Long tutorials (link to docs instead)

## Symlink Convention

For compatibility across tools, create both files:

```bash
# AGENTS.md is the primary file
# CLAUDE.md symlinks to it for Claude Code compatibility
ln -sf AGENTS.md CLAUDE.md
```

## Progressive Disclosure

Large projects can have `AGENTS.md` files in subdirectories, each documenting that subsystem's boundaries and conventions:

```
AGENTS.md                  # Project-wide conventions
src/api/AGENTS.md          # API-specific patterns
src/frontend/AGENTS.md     # Frontend-specific patterns
```

## Relationship to System Prompt

In aloop's **section mode**, the AGENTS.md body is appended to the system prompt under `# Project Context`.

In **template mode**, the AGENTS.md body is available as `{{agents_md}}` — you choose where it appears in the prompt. This is useful when you want the project context in a specific position relative to identity and tools.

## Size Guidelines

Keep it under 40,000 characters. The model's context window is shared between the system prompt, AGENTS.md content, conversation history, and tool results. A massive AGENTS.md leaves less room for the actual work.

For aloop specifically, the AGENTS.md content becomes part of the system prompt (cached), so it doesn't consume context on every turn — but it still counts against the model's window.
