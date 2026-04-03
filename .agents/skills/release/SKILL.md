---
name: release
description: Cut a new aloop release — bump version, update changelog, commit, tag, push.
---

# Release Skill

Cut a new release of aloop. Follow these steps exactly.

## Arguments

`$ARGUMENTS` should be the version number (e.g. `0.2.0`) or a bump type (`patch`, `minor`, `major`).

## Steps

### 1. Determine version

- If a semver was given (e.g. `0.2.0`), use it directly.
- If a bump type was given, read the current version from `src/aloop/__init__.py` and bump accordingly:
  - `patch`: 0.1.0 → 0.1.1
  - `minor`: 0.1.0 → 0.2.0
  - `major`: 0.1.0 → 1.0.0
- Print: "Releasing vX.Y.Z (was vA.B.C)"

### 2. Review commits since last release

Run `git log $(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD)..HEAD --oneline` to see all commits since the last tag (or since the beginning if no tags exist).

Review each commit and make sure the `[Unreleased]` section in `CHANGELOG.md` accurately reflects what changed. Add missing entries, remove entries that were reverted, and clean up wording.

Group entries under the standard headings:
- **Added** — new features
- **Changed** — changes to existing functionality
- **Fixed** — bug fixes
- **Removed** — removed features

### 3. Update CHANGELOG.md

Move the `[Unreleased]` section to the new version:

```markdown
## [Unreleased]

## [X.Y.Z] - YYYY-MM-DD

### Added
- ...
```

The `[Unreleased]` section should be empty (just the heading) after this step.

### 4. Bump version in source

Edit `src/aloop/__init__.py` and change `__version__ = "..."` to the new version. This is the single source of truth — `pyproject.toml` reads it via hatchling.

### 5. Run tests

```bash
uv run pytest tests/ -q
```

All tests must pass. Do not proceed if any fail.

### 6. Commit

```bash
git add CHANGELOG.md src/aloop/__init__.py
git commit -m "release vX.Y.Z"
```

### 7. Tag

```bash
git tag vX.Y.Z
```

### 8. Push

```bash
git push origin master --tags
```

### 9. Verify

- Confirm the tag is on GitHub: `gh browse`
- Test install from the new tag: `uv tool install git+https://github.com/zackham/aloop.git@vX.Y.Z`
- Run `aloop --version` and confirm it matches

### 10. Post-release

Print a summary:
```
Released vX.Y.Z
  Tag: vX.Y.Z
  Commits: N
  Changelog entries: N added, N changed, N fixed, N removed
```
