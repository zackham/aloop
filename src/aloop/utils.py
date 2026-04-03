"""Shared utilities for aloop."""

from __future__ import annotations

import json
from pathlib import Path


def strip_json_comments(text: str) -> str:
    """Strip // and # line comments from JSON text.

    Only strips comments that appear outside of quoted strings.
    Handles escaped quotes inside strings correctly.
    """
    result: list[str] = []
    i = 0
    length = len(text)

    while i < length:
        ch = text[i]

        # String literal — consume until closing quote
        if ch == '"':
            result.append(ch)
            i += 1
            while i < length:
                c = text[i]
                result.append(c)
                if c == '\\':
                    # Escaped character — consume next char too
                    i += 1
                    if i < length:
                        result.append(text[i])
                elif c == '"':
                    break
                i += 1
            i += 1
            continue

        # // comment — skip to end of line
        if ch == '/' and i + 1 < length and text[i + 1] == '/':
            # Skip until newline (keep the newline to preserve line structure)
            i += 2
            while i < length and text[i] != '\n':
                i += 1
            continue

        # # comment — skip to end of line
        if ch == '#':
            i += 1
            while i < length and text[i] != '\n':
                i += 1
            continue

        result.append(ch)
        i += 1

    return ''.join(result)


def load_jsonc(path: Path) -> dict:
    """Load a JSONC file (JSON with // and # comments), returning empty dict on missing/invalid."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(strip_json_comments(text))
    except (OSError, json.JSONDecodeError):
        return {}
