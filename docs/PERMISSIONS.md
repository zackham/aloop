# Permissions

aloop's permission system uses **tool sets as the security boundary**. Instead of filtering bash commands, modes without bash get dedicated read-only tools that are inherently safe.

## Security Model

Two tiers:

1. **Tool set selection** — the real security decision. A mode's `tools` list determines what the agent can do. Modes without `bash`: path restrictions are meaningful because the only way to touch files is through aloop's tools. Modes with `bash`: path restrictions are cosmetic — bash can bypass everything.

2. **Path restrictions** — enforced guardrails for bash-less modes, gentle bumpers for bash modes that catch honest mistakes but don't stop adversarial behavior.

**Default: no restrictions.** If there's no `permissions` key in config, everything is allowed. You only add constraints when you need them.

## Tool Sets

aloop ships three built-in tool sets:

| Set | Tools | Use Case |
|-----|-------|----------|
| `CODING_TOOLS` (default) | read_file, write_file, edit_file, bash, load_skill | Normal development. Full access. |
| `READONLY_TOOLS` | read_file, grep, find, ls, load_skill | Safe exploration. No shell, no writes. |
| `ALL_TOOLS` | Everything in both sets | When you want exploration tools alongside full access. |

The default mode uses `CODING_TOOLS`. The read-only tools (grep, find, ls) are only available when a mode explicitly includes them.

## Read-Only Tools

Three tools for safe codebase exploration, modeled on [Pi's](https://github.com/openclaw/openclaw) `readOnlyTools`:

### grep

Search file contents via ripgrep. Respects `.gitignore`.

```
grep(pattern, path?, glob?, ignore_case?, literal?, context?, limit?)
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `pattern` | string | (required) | Regex or literal search pattern |
| `path` | string | project root | Directory or file to search |
| `glob` | string | none | Filter files, e.g. `*.py`, `**/*.ts` |
| `ignore_case` | bool | false | Case-insensitive search |
| `literal` | bool | false | Treat pattern as literal string |
| `context` | int | 0 | Lines of context around matches |
| `limit` | int | 100 | Max matches returned |

Output truncated at 100 matches or 50KB. Per-line truncation at 500 chars. Requires ripgrep (`rg`) installed on the system.

### find

Find files by glob pattern. Respects `.gitignore`. Uses `fd` when available, falls back to Python glob.

```
find(pattern, path?, limit?)
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `pattern` | string | (required) | Glob pattern, e.g. `*.py`, `**/*.json` |
| `path` | string | project root | Directory to search |
| `limit` | int | 1000 | Max results |

Output truncated at 1000 results or 50KB. Python glob fallback filters `node_modules`, `.git`, `__pycache__`, `.venv`.

### ls

List directory contents. Pure Python, no shell. Single level, no recursion.

```
ls(path?, limit?)
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `path` | string | project root | Directory to list |
| `limit` | int | 500 | Max entries |

Entries sorted case-insensitive with `/` suffix for directories. Includes dotfiles. Broken symlinks silently skipped.

## Permission Config

Config lives in `.aloop/config.json` under a `permissions` key. No key = no restrictions.

```jsonc
{
  "permissions": {
    "paths": {
      // Deny globs — blocked for ALL file operations (read + write)
      "deny": [".env", "**/*.key", "**/*.pem"],

      // Project containment (default: true = allow outside)
      "allow_outside_project": false,

      // Extra dirs when containment is enabled
      "additional_dirs": ["~/work/shared-lib"],

      // Write restrictions — only these paths are writable
      "write": ["src/**", "tests/**"]
    }
  }
}
```

### Path restriction fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `paths.deny` | `list[string]` | `[]` | Glob patterns denied for all file operations |
| `paths.allow_outside_project` | `bool` | `true` | Whether file tools can access paths outside the project root |
| `paths.additional_dirs` | `list[string]` | `[]` | Extra directories allowed when containment is enabled |
| `paths.write` | `list[string]` | `[]` (all) | If set, only these glob patterns are writable |

### Resolution order

1. **Hardcoded denies** (non-overridable): `.git/**` writes, `.aloop/config.json` writes, catastrophic bash commands
2. **Mode-level permissions** (most specific)
3. **Project-level permissions** (`.aloop/config.json`)
4. **Default** (no restrictions)

Deny lists merge additively across levels. Other fields: mode overrides project.

## Per-Mode Permissions

Modes can define their own permissions alongside their tool set:

```jsonc
{
  "modes": {
    "review": {
      "model": "gemini-3-flash",
      "tools": ["read_file", "grep", "find", "ls", "load_skill"]
      // No bash. Tool set IS the security boundary.
    },
    "implement": {
      "tools": ["*"],  // All tools
      "permissions": {
        "paths": { "write": ["src/**", "tests/**"] }
      }
    }
  }
}
```

Use `"tools": ["*"]` for all available tools (including grep/find/ls).

## Hardcoded Safety Net

Always active, cannot be overridden by config. This is catastrophe prevention, not security.

**Write denies:**
- `.git/**` — repository internals
- `.aloop/config.json` — own config

**Bash denies:**
- `rm -rf /`, `rm -rf ~`, `rm -rf /*`
- Fork bomb `:(){ :|:& };:`
- `mkfs`, `dd if=`

## PermissionDenied

`PermissionDenied` is a subclass of `ToolRejected`. When raised, the tool call is skipped and the reason is returned to the model as an error. The model can then adapt (e.g., use a different tool or explain why it can't proceed).

```python
from aloop import PermissionDenied, ToolRejected

# PermissionDenied is a ToolRejected
assert issubclass(PermissionDenied, ToolRejected)
```

## Design Philosophy

- **Tool sets, not tool filtering.** Don't give an agent bash and try to make bash safe — give it different tools that are inherently safe. Inspired by [Pi](https://github.com/openclaw/openclaw)'s `codingTools` vs `readOnlyTools`.
- **Honest about bash.** If bash is in the tool set, it's unrestricted. No pretending otherwise. The hardcoded safety net catches accidental catastrophes, not adversarial agents.
- **Default yolo.** No permissions key = no restrictions. You opt into constraints when you need them.
- **Composable with hooks.** Config permissions run at priority 0 (before all hooks). User hooks at priority 5+ can add further restrictions (VITA uses capability tokens and firebreaks this way).
