# File Resolution

aloop discovers project files using fallback chains. First match wins — files are not merged. This page is the single source of truth for what gets loaded from where.

## Instruction file

The project instruction file provides context about your project to the agent. One file is loaded.

```
ALOOP.md → AGENTS.md → .agents/AGENTS.md → CLAUDE.md → .claude/CLAUDE.md
```

| File | When to use |
|------|-------------|
| `ALOOP.md` | aloop-specific context — use when you control the full system prompt (template mode) and need different instructions than what other tools see |
| `AGENTS.md` | Cross-tool convention (Codex, OpenCode, etc.) |
| `.agents/AGENTS.md` | Same convention, tucked into a dotdir |
| `CLAUDE.md` | Claude Code convention |
| `.claude/CLAUDE.md` | Claude Code convention, tucked into a dotdir |

**First match wins.** If you have both `ALOOP.md` and `AGENTS.md`, only `ALOOP.md` is loaded. The others are ignored (but noted in `aloop config show`).

### Why ALOOP.md exists

When you use template mode (full system prompt control), your project context is different from what you'd put in `AGENTS.md`. Your template already has behavioral instructions — the instruction file just needs project-specific facts. `ALOOP.md` lets you maintain a lean aloop-specific file alongside the standard `AGENTS.md` that other tools use.

If you don't need this distinction, just use `AGENTS.md` — it works everywhere.

### Where it goes in the prompt

- **Section mode**: appended under a `# Project Context` heading
- **Template mode**: available as `{{agents_md}}` — inserted wherever you place the variable, with no heading added (your file's content goes in as-is, frontmatter stripped)

## Skills

Skills are markdown instruction files that the agent loads on demand. Skills are **merged** across all found directories — not first-match-wins.

### Discovery order (all scanned, merged by name)

```
Project:
  .aloop/skills/    (highest priority)
  .agents/skills/
  .claude/skills/

Global:
  ~/.aloop/skills/  (lowest priority)
```

Every directory that exists is scanned. If two directories contain a skill with the same name, the higher-priority one wins. Project always beats global.

### Skill structure

```
.aloop/skills/
  deploy/
    SKILL.md          # frontmatter (name, description) + full instructions
  migrate/
    SKILL.md
```

Short descriptions appear in the system prompt (~1% of context). Full content loaded on demand via the `load_skill` tool.

### Disabling skills

In `.aloop/config.json`:

```jsonc
{
    "disabled_skills": ["some_global_skill"]
}
```

Disabled skills are excluded from discovery entirely.

## Hooks

Hook files are Python modules in `hooks/` directories. Hooks are **merged** — both global and project hooks run.

### Discovery order (both run)

```
Global:   ~/.aloop/hooks/     (runs first)
Project:  .aloop/hooks/       (runs second)
```

Both scopes execute. Within each scope, hooks run in alphabetical order by filename.

If a hook file with the same name exists in both global and project directories, the project version replaces the global version (project wins on collision).

### Hook structure

```
.aloop/hooks/
  __init__.py         # hook decorator + discovery (required)
  permissions.py      # @hook("before_tool") functions
  logging.py          # @hook("on_turn_end") functions
```

Files starting with `_` are skipped. Every `.py` file is loaded and scanned for `@hook` decorators.

### Disabling hooks

In `.aloop/config.json`:

```jsonc
{
    "disabled_hooks": ["some_global_hook_filename"]
}
```

## Config

Configuration uses **deep merge** — global provides defaults, project overrides.

### Discovery

```
Global:   ~/.aloop/config.json    (defaults)
Project:  .aloop/config.json      (overrides)
```

Both are loaded. Project values override global values on key collision. Nested objects are recursively merged.

Example:

```jsonc
// ~/.aloop/config.json (global)
{
    "provider": "openrouter",
    "modes": {
        "review": {"tools": ["read_file", "bash"]}
    }
}

// .aloop/config.json (project)
{
    "modes": {
        "review": {"model": "x-ai/grok-4.1-fast"}
    }
}

// Merged result:
{
    "provider": "openrouter",
    "modes": {
        "review": {"model": "x-ai/grok-4.1-fast"}  // note: tools from global is gone
    }
}
```

**Caveat**: deep merge replaces entire nested objects at the leaf level. In the example above, the project's `review` mode replaces the global's `review` mode entirely — they don't combine. This is intentional (predictable > magical).

## Global-only files

These live exclusively in `~/.aloop/` and are never project-scoped:

| File | Purpose |
|------|---------|
| `credentials.json` | API keys (per-provider) |
| `providers.json` | Custom provider definitions |
| `models.json` | Model aliases with cost metadata |
| `sessions/` | Persisted session conversation history |
| `state.json` | Last session ID for `-c` flag |

## Debugging

```bash
aloop config show
```

Shows:
- Which instruction file was loaded (and which candidates were skipped)
- Skills grouped by source directory
- Hooks by scope (global / project)
- Merged config result
- Disabled hooks and skills

Example output:

```
aloop configuration

  project root:    /home/user/my-project
  global config:   ~/.aloop/config.json
  project config:  .aloop/config.json
  instructions:    AGENTS.md
                   (CLAUDE.md also exists but lower priority)
  prompt mode:     section (default)

  skills:
    [project] .aloop/skills
      deploy, migrate
    [global] ~/.aloop/skills
      lint-check

  hooks:
    [global] ~/.aloop/hooks
      logging
    [project] .aloop/hooks
      permissions, firebreaks

  merged config:
    provider: openrouter
    modes: {review: {...}}
```

## Summary

| Resource | Strategy | Scope |
|----------|----------|-------|
| Instructions | First match wins | Project only |
| Skills | Merge all dirs, higher priority wins on name collision | Global + project |
| Hooks | Both run (global first, project second), same-name: project replaces | Global + project |
| Config | Deep merge, project overrides global on key collision | Global + project |
| Credentials, sessions, providers, models | Global only | Global only |
