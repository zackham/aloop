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


def _fork_index_path() -> Path:
    return _sessions_dir() / "_fork_index.json"


def _load_fork_index() -> dict[str, list[str]] | None:
    path = _fork_index_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("version") != 1:
        return None
    ptc = data.get("parent_to_children")
    if not isinstance(ptc, dict):
        return None
    return ptc


def _save_fork_index(index: dict[str, list[str]]) -> None:
    try:
        path = _fork_index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "parent_to_children": index}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _rebuild_fork_index() -> dict[str, list[str]]:
    search_dir = _sessions_dir()
    index: dict[str, list[str]] = {}
    if search_dir.exists():
        for ctx_file in search_dir.rglob("*.context.json"):
            try:
                data = json.loads(ctx_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            parent = data.get("fork_from")
            sid = data.get("session_id")
            if parent and sid:
                index.setdefault(parent, []).append(sid)
    _save_fork_index(index)
    return index


@dataclass
class AgentSession:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    last_compaction: CompactionEntry | None = None
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    fork_from: str | None = None
    fork_turn_id: str | None = None

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
            "turn_id": msg.get("turn_id"),
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
            "fork_from": self.fork_from,
            "fork_turn_id": self.fork_turn_id,
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
            fork_from=data.get("fork_from"),
            fork_turn_id=data.get("fork_turn_id"),
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

    @classmethod
    def fork(cls, parent_session_id: str, fork_turn_id: str) -> "AgentSession":
        """Create a new session forked from a parent at a specific turn.

        The child starts with empty messages. On resolve_messages(), it
        will include parent messages up to and including all messages
        with the given turn_id, then its own messages.
        """
        import uuid

        parent = cls.load(parent_session_id)
        if parent is None:
            raise ValueError(f"Parent session {parent_session_id!r} not found")

        # Validate turn_id exists in the parent's resolved message history
        resolved = parent.resolve_messages()
        if not any(m.get("turn_id") == fork_turn_id for m in resolved):
            raise ValueError(
                f"turn_id {fork_turn_id!r} not found in session {parent_session_id!r}"
            )

        child = cls(
            session_id=uuid.uuid4().hex[:16],
            fork_from=parent_session_id,
            fork_turn_id=fork_turn_id,
        )
        child.save_context()

        index = _load_fork_index()
        if index is None:
            index = _rebuild_fork_index()
        else:
            index.setdefault(parent_session_id, []).append(child.session_id)
            _save_fork_index(index)

        return child

    def resolve_messages(self, _depth: int = 0) -> list[dict]:
        """Walk the parent chain and return the full message history.

        For a non-forked session, returns self.messages.
        For a forked session, returns parent prefix (up to and including
        all messages with fork_turn_id) + self.messages.
        Auto-materializes at depth >= 10 to bound chain walks.
        """
        if self.fork_from is None:
            return list(self.messages)

        if _depth >= 10:
            self.materialize()
            return list(self.messages)

        parent = AgentSession.load(self.fork_from)
        if parent is None:
            raise ValueError(
                f"Parent session {self.fork_from!r} not found (broken fork chain)"
            )

        parent_messages = parent.resolve_messages(_depth + 1)

        # Find the cut point: after the LAST message with fork_turn_id
        cut = -1
        for i, m in enumerate(parent_messages):
            if m.get("turn_id") == self.fork_turn_id:
                cut = i
        if cut == -1:
            raise ValueError(
                f"turn_id {self.fork_turn_id!r} not found in parent chain"
            )

        return parent_messages[: cut + 1] + list(self.messages)

    def materialize(self) -> None:
        """Flatten the fork chain into this session's messages."""
        old_parent = self.fork_from
        full = self.resolve_messages()
        self.messages = full
        self.fork_from = None
        self.fork_turn_id = None
        self.save_context()

        if old_parent is not None:
            index = _load_fork_index()
            if index is not None:
                children = index.get(old_parent, [])
                if self.session_id in children:
                    children.remove(self.session_id)
                    if not children:
                        del index[old_parent]
                    _save_fork_index(index)

    def fork_depth(self) -> int:
        """Count the depth of the fork chain (0 for non-forked sessions)."""
        depth = 0
        session = self
        while session.fork_from is not None:
            depth += 1
            parent = AgentSession.load(session.fork_from)
            if parent is None:
                break
            session = parent
        return depth

    def children(self) -> list[str]:
        """Return session IDs whose fork_from == self.session_id (cached via fork index)."""
        index = _load_fork_index()
        if index is None:
            index = _rebuild_fork_index()
        return list(index.get(self.session_id, []))


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
                "fork_from": data.get("fork_from"),
                "fork_turn_id": data.get("fork_turn_id"),
            }
        )

    return sorted(sessions, key=lambda item: item.get("last_active") or 0, reverse=True)


def gc_sessions(max_age_seconds: float = 604800) -> list[str]:
    """Garbage-collect expired sessions.

    Materializes children of expired sessions before deleting them,
    so no fork chain is left broken. Returns list of deleted session_ids.
    """
    search_dir = _sessions_dir()
    if not search_dir.exists():
        return []

    # Load minimal data from all sessions
    all_sessions: list[dict] = []
    for ctx_file in search_dir.rglob("*.context.json"):
        try:
            data = json.loads(ctx_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        all_sessions.append(data)

    # Build parent -> children index (from fork index cache)
    index = _load_fork_index()
    if index is None:
        index = _rebuild_fork_index()
    children_of = dict(index)  # copy so we don't mutate during iteration

    now = time.time()
    # Sort oldest first
    all_sessions.sort(key=lambda s: s.get("last_active") or 0)

    deleted: list[str] = []
    for s in all_sessions:
        last_active = s.get("last_active") or 0
        if now - last_active < max_age_seconds:
            continue

        sid = s["session_id"]

        # Materialize any children before deleting
        for child_id in children_of.get(sid, []):
            child = AgentSession.load(child_id)
            if child and child.fork_from == sid:
                child.materialize()

        # Delete files
        ctx_path = search_dir / f"{sid}.context.json"
        log_path = search_dir / f"{sid}.log.jsonl"
        if ctx_path.exists():
            ctx_path.unlink()
        if log_path.exists():
            log_path.unlink()

        deleted.append(sid)

    if deleted:
        for sid in deleted:
            index.pop(sid, None)
            for parent_id, child_list in list(index.items()):
                if sid in child_list:
                    child_list.remove(sid)
                    if not child_list:
                        del index[parent_id]
        _save_fork_index(index)

    return deleted
