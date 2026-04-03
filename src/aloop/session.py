"""Persistent inference sessions for long-running agents."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .compaction import CompactionEntry

from . import get_project_root


def _sessions_dir() -> Path:
    return Path.home() / ".aloop" / "sessions"


@dataclass
class AgentSession:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    last_compaction: CompactionEntry | None = None
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    @property
    def session_dir(self) -> Path:
        return _sessions_dir()

    @property
    def log_path(self) -> Path:
        return self.session_dir / f"{self.session_id}.log.jsonl"

    @property
    def context_path(self) -> Path:
        return self.session_dir / f"{self.session_id}.context.json"

    def ensure_dir(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def log_message(self, msg: dict) -> None:
        self.ensure_dir()
        entry = {
            "timestamp": time.time(),
            "role": msg.get("role"),
            "content": str(msg.get("content", ""))[:5000],
            "tool_calls": bool(msg.get("tool_calls")),
        }
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def log_event(self, event_type: str, data: dict | None = None) -> None:
        self.ensure_dir()
        entry = {
            "timestamp": time.time(),
            "event": event_type,
            "data": data,
        }
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def save_context(self) -> None:
        self.ensure_dir()
        payload = {
            "session_id": self.session_id,
            "messages": self.messages,
            "last_compaction": asdict(self.last_compaction) if self.last_compaction else None,
            "created_at": self.created_at,
            "last_active": time.time(),
        }

        tmp_path = self.context_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self.context_path)

    def is_stale(
        self,
        max_age_seconds: float = 14400,
        max_messages: int = 100,
    ) -> bool:
        """Check if session should be auto-cleared."""
        if time.time() - self.last_active > max_age_seconds:
            return True
        if len(self.messages) > max_messages:
            return True
        return False

    def clear(self) -> None:
        self.messages = []
        self.last_compaction = None
        self.save_context()

    @classmethod
    def load(cls, session_id: str) -> "AgentSession | None":
        path = _sessions_dir() / f"{session_id}.context.json"
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

        session = cls(
            session_id=data.get("session_id", session_id),
            messages=data.get("messages", []),
            created_at=data.get("created_at", time.time()),
            last_active=data.get("last_active", time.time()),
        )

        last_compaction = data.get("last_compaction")
        if isinstance(last_compaction, dict):
            try:
                session.last_compaction = CompactionEntry(**last_compaction)
            except TypeError:
                session.last_compaction = None

        return session

    @classmethod
    def get_or_create(cls, session_id: str) -> "AgentSession":
        existing = cls.load(session_id)
        if existing is not None:
            return existing
        return cls(session_id=session_id)


def list_sessions() -> list[dict]:
    sessions: list[dict] = []
    search_dir = _sessions_dir()

    if not search_dir.exists():
        return sessions

    for ctx_file in search_dir.rglob("*.context.json"):
        try:
            data = json.loads(ctx_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        sessions.append(
            {
                "session_id": data.get("session_id"),
                "message_count": len(data.get("messages", [])),
                "last_active": data.get("last_active"),
                "created_at": data.get("created_at"),
            }
        )

    return sorted(sessions, key=lambda item: item.get("last_active") or 0, reverse=True)
