"""Tests for session forking — turn IDs, fork/resolve/materialize, GC."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from aloop.session import (
    AgentSession,
    gc_sessions,
    list_sessions,
    _fork_index_path,
    _load_fork_index,
    _rebuild_fork_index,
    _save_fork_index,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_messages(*turns: list[tuple[str, str, str]]) -> list[dict]:
    """Build a message list from (role, content, turn_id) tuples."""
    return [{"role": r, "content": c, "turn_id": t} for r, c, t in turns]


def _make_session(
    tmp_path,
    session_id: str,
    messages: list[dict],
    *,
    fork_from: str | None = None,
    fork_turn_id: str | None = None,
    last_active: float | None = None,
) -> AgentSession:
    """Create and save a session to tmp_path."""
    s = AgentSession(
        session_id=session_id,
        messages=messages,
        fork_from=fork_from,
        fork_turn_id=fork_turn_id,
    )
    if last_active is not None:
        s.last_active = last_active
    s.save_context()
    return s


# ---------------------------------------------------------------------------
# Turn ID persistence
# ---------------------------------------------------------------------------


class TestTurnIdPersistence:
    def test_messages_have_turn_id(self, tmp_path):
        """Messages with turn_ids survive save/load roundtrip."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            msgs = _make_messages(
                ("user", "hello", "aaa111"),
                ("assistant", "hi there", "aaa111"),
                ("user", "next", "bbb222"),
                ("assistant", "response", "bbb222"),
            )
            s = _make_session(tmp_path, "s1", msgs)

            loaded = AgentSession.load("s1")
            assert loaded is not None
            for orig, reloaded in zip(msgs, loaded.messages):
                assert reloaded["turn_id"] == orig["turn_id"]

    def test_all_messages_in_turn_share_turn_id(self, tmp_path):
        """All messages within one turn share the same turn_id."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            turn_id = "turn_abc"
            msgs = [
                {"role": "user", "content": "do stuff", "turn_id": turn_id},
                {"role": "assistant", "content": None, "turn_id": turn_id, "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "grep", "arguments": "{}"}}]},
                {"role": "tool", "content": "result", "tool_call_id": "tc1", "turn_id": turn_id},
                {"role": "assistant", "content": "done", "turn_id": turn_id},
            ]
            _make_session(tmp_path, "s1", msgs)
            loaded = AgentSession.load("s1")
            assert all(m.get("turn_id") == turn_id for m in loaded.messages)


# ---------------------------------------------------------------------------
# Turn ID in log entries
# ---------------------------------------------------------------------------


class TestTurnIdLogging:
    def test_log_message_includes_turn_id(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            s = AgentSession(session_id="log_test")
            msg = {"role": "user", "content": "hello", "turn_id": "tid_123"}
            s.log_message(msg)

            entries = [
                json.loads(line)
                for line in s.log_path.read_text().strip().split("\n")
            ]
            assert len(entries) == 1
            assert entries[0]["turn_id"] == "tid_123"

    def test_log_message_turn_id_none_when_absent(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            s = AgentSession(session_id="log_test2")
            msg = {"role": "user", "content": "hello"}
            s.log_message(msg)

            entries = [
                json.loads(line)
                for line in s.log_path.read_text().strip().split("\n")
            ]
            assert entries[0]["turn_id"] is None


# ---------------------------------------------------------------------------
# Fork basics
# ---------------------------------------------------------------------------


class TestForkBasics:
    def test_fork_creates_child_session(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            msgs = _make_messages(
                ("user", "hi", "t1"),
                ("assistant", "hello", "t1"),
            )
            _make_session(tmp_path, "parent", msgs)

            child = AgentSession.fork("parent", "t1")
            assert child.fork_from == "parent"
            assert child.fork_turn_id == "t1"
            assert child.session_id != "parent"
            # Child exists on disk
            assert AgentSession.load(child.session_id) is not None

    def test_fork_child_starts_empty(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            msgs = _make_messages(
                ("user", "hi", "t1"),
                ("assistant", "hello", "t1"),
            )
            _make_session(tmp_path, "parent", msgs)

            child = AgentSession.fork("parent", "t1")
            assert child.messages == []

    def test_resolve_includes_parent_prefix(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent_msgs = _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
                ("user", "q2", "t2"),
                ("assistant", "a2", "t2"),
            )
            _make_session(tmp_path, "parent", parent_msgs)

            child = AgentSession.fork("parent", "t2")
            child.messages = _make_messages(
                ("user", "q3", "t3"),
                ("assistant", "a3", "t3"),
            )
            child.save_context()

            resolved = child.resolve_messages()
            assert len(resolved) == 6  # 4 parent + 2 child
            assert resolved[0]["content"] == "q1"
            assert resolved[-1]["content"] == "a3"

    def test_resolve_excludes_after_fork_point(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent_msgs = _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
                ("user", "q2", "t2"),
                ("assistant", "a2", "t2"),
                ("user", "q3", "t3"),
                ("assistant", "a3", "t3"),
            )
            _make_session(tmp_path, "parent", parent_msgs)

            child = AgentSession.fork("parent", "t1")
            resolved = child.resolve_messages()
            # Only t1 messages from parent, no t2/t3
            assert len(resolved) == 2
            assert all(m["turn_id"] == "t1" for m in resolved)

    def test_fork_nonexistent_session_raises(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            with pytest.raises(ValueError, match="not found"):
                AgentSession.fork("nonexistent", "t1")

    def test_fork_nonexistent_turn_raises(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            msgs = _make_messages(
                ("user", "hi", "t1"),
                ("assistant", "hello", "t1"),
            )
            _make_session(tmp_path, "parent", msgs)

            with pytest.raises(ValueError, match="turn_id.*not found"):
                AgentSession.fork("parent", "nonexistent_turn")


# ---------------------------------------------------------------------------
# Nested forks
# ---------------------------------------------------------------------------


class TestNestedForks:
    def test_nested_fork_two_levels(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            p_msgs = _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
            )
            _make_session(tmp_path, "root", p_msgs)

            child = AgentSession.fork("root", "t1")
            child.messages = _make_messages(
                ("user", "q2", "t2"),
                ("assistant", "a2", "t2"),
            )
            child.save_context()

            grandchild = AgentSession.fork(child.session_id, "t2")
            grandchild.messages = _make_messages(
                ("user", "q3", "t3"),
                ("assistant", "a3", "t3"),
            )
            grandchild.save_context()

            resolved = grandchild.resolve_messages()
            assert len(resolved) == 6
            assert [m["content"] for m in resolved] == ["q1", "a1", "q2", "a2", "q3", "a3"]

    def test_nested_fork_three_levels(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "L0", _make_messages(("user", "L0", "t0"), ("assistant", "A0", "t0")))

            L1 = AgentSession.fork("L0", "t0")
            L1.messages = _make_messages(("user", "L1", "t1"), ("assistant", "A1", "t1"))
            L1.save_context()

            L2 = AgentSession.fork(L1.session_id, "t1")
            L2.messages = _make_messages(("user", "L2", "t2"), ("assistant", "A2", "t2"))
            L2.save_context()

            L3 = AgentSession.fork(L2.session_id, "t2")
            L3.messages = _make_messages(("user", "L3", "t3"), ("assistant", "A3", "t3"))
            L3.save_context()

            resolved = L3.resolve_messages()
            assert len(resolved) == 8
            contents = [m["content"] for m in resolved]
            assert contents == ["L0", "A0", "L1", "A1", "L2", "A2", "L3", "A3"]

    def test_fork_depth_correct(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "root", _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")))

            c1 = AgentSession.fork("root", "t1")
            c1.messages = _make_messages(("user", "q2", "t2"), ("assistant", "a2", "t2"))
            c1.save_context()

            c2 = AgentSession.fork(c1.session_id, "t2")

            root = AgentSession.load("root")
            assert root.fork_depth() == 0
            assert c1.fork_depth() == 1
            assert c2.fork_depth() == 2


# ---------------------------------------------------------------------------
# Materialize
# ---------------------------------------------------------------------------


class TestMaterialize:
    def test_materialize_flattens_chain(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent_msgs = _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
            )
            _make_session(tmp_path, "parent", parent_msgs)

            child = AgentSession.fork("parent", "t1")
            child.messages = _make_messages(
                ("user", "q2", "t2"),
                ("assistant", "a2", "t2"),
            )
            child.save_context()

            child.materialize()
            assert child.fork_from is None
            assert child.fork_turn_id is None
            assert len(child.messages) == 4
            assert child.messages[0]["content"] == "q1"
            assert child.messages[-1]["content"] == "a2"

    def test_materialize_idempotent(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent_msgs = _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
            )
            _make_session(tmp_path, "parent", parent_msgs)

            child = AgentSession.fork("parent", "t1")
            child.messages = _make_messages(("user", "q2", "t2"),)
            child.save_context()

            child.materialize()
            msgs_after_first = list(child.messages)

            child.materialize()
            assert child.messages == msgs_after_first
            assert child.fork_from is None

    def test_materialize_save_load_roundtrip(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(
                tmp_path, "p",
                _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")),
            )
            child = AgentSession.fork("p", "t1")
            child.messages = _make_messages(("user", "q2", "t2"),)
            child.save_context()

            child.materialize()

            reloaded = AgentSession.load(child.session_id)
            assert reloaded.fork_from is None
            assert reloaded.fork_turn_id is None
            assert len(reloaded.messages) == 3

    def test_resolve_after_materialize(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(
                tmp_path, "p",
                _make_messages(("user", "q1", "t1"), ("assistant", "a1", "t1")),
            )
            child = AgentSession.fork("p", "t1")
            child.messages = _make_messages(("user", "q2", "t2"),)
            child.save_context()

            child.materialize()
            resolved = child.resolve_messages()
            assert len(resolved) == 3
            assert resolved == child.messages


# ---------------------------------------------------------------------------
# Both branches continue
# ---------------------------------------------------------------------------


class TestBranchesContinue:
    def test_parent_continues_after_fork(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent_msgs = _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
            )
            parent = _make_session(tmp_path, "parent", parent_msgs)

            _child = AgentSession.fork("parent", "t1")

            # Parent adds more messages
            parent.messages.extend(_make_messages(
                ("user", "q2", "t2"),
                ("assistant", "a2", "t2"),
            ))
            parent.save_context()

            reloaded = AgentSession.load("parent")
            assert len(reloaded.messages) == 4

    def test_child_continues_after_fork(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(
                tmp_path, "parent",
                _make_messages(("user", "q1", "t1"), ("assistant", "a1", "t1")),
            )

            child = AgentSession.fork("parent", "t1")
            child.messages = _make_messages(
                ("user", "q2", "t2"),
                ("assistant", "a2", "t2"),
            )
            child.save_context()

            child.messages.extend(_make_messages(
                ("user", "q3", "t3"),
                ("assistant", "a3", "t3"),
            ))
            child.save_context()

            resolved = child.resolve_messages()
            assert len(resolved) == 6

    def test_branches_diverge_independently(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent_msgs = _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
            )
            parent = _make_session(tmp_path, "parent", parent_msgs)

            child = AgentSession.fork("parent", "t1")
            child.messages = _make_messages(
                ("user", "child_q", "tc1"),
                ("assistant", "child_a", "tc1"),
            )
            child.save_context()

            # Parent diverges
            parent.messages.extend(_make_messages(
                ("user", "parent_q", "tp1"),
                ("assistant", "parent_a", "tp1"),
            ))
            parent.save_context()

            # Child should still see its own branch
            child_resolved = child.resolve_messages()
            parent_resolved = parent.resolve_messages()

            assert [m["content"] for m in child_resolved] == ["q1", "a1", "child_q", "child_a"]
            assert [m["content"] for m in parent_resolved] == ["q1", "a1", "parent_q", "parent_a"]


# ---------------------------------------------------------------------------
# Concurrent forks
# ---------------------------------------------------------------------------


class TestConcurrentForks:
    def test_concurrent_forks_same_parent_same_turn(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(
                tmp_path, "parent",
                _make_messages(
                    ("user", "q1", "t1"),
                    ("assistant", "a1", "t1"),
                    ("user", "q2", "t2"),
                    ("assistant", "a2", "t2"),
                ),
            )

            c1 = AgentSession.fork("parent", "t1")
            c2 = AgentSession.fork("parent", "t1")

            assert c1.session_id != c2.session_id
            c1.messages = _make_messages(("user", "branch_a", "ta"),)
            c2.messages = _make_messages(("user", "branch_b", "tb"),)
            c1.save_context()
            c2.save_context()

            r1 = c1.resolve_messages()
            r2 = c2.resolve_messages()
            # Both share the same prefix
            assert r1[:2] == r2[:2]
            # But diverge
            assert r1[-1]["content"] == "branch_a"
            assert r2[-1]["content"] == "branch_b"

    def test_concurrent_forks_different_turns(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(
                tmp_path, "parent",
                _make_messages(
                    ("user", "q1", "t1"),
                    ("assistant", "a1", "t1"),
                    ("user", "q2", "t2"),
                    ("assistant", "a2", "t2"),
                ),
            )

            c1 = AgentSession.fork("parent", "t1")
            c2 = AgentSession.fork("parent", "t2")

            r1 = c1.resolve_messages()
            r2 = c2.resolve_messages()

            assert len(r1) == 2  # only t1
            assert len(r2) == 4  # t1 + t2


# ---------------------------------------------------------------------------
# Depth limit
# ---------------------------------------------------------------------------


class TestDepthLimit:
    def test_auto_materialize_at_depth_10(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            # Build a chain of depth 11
            prev_id = "root"
            prev_turn_id = "t_root"
            _make_session(
                tmp_path, prev_id,
                _make_messages(("user", "root_q", prev_turn_id), ("assistant", "root_a", prev_turn_id)),
            )

            child_ids: list[str] = []
            for i in range(11):
                child = AgentSession.fork(prev_id, prev_turn_id)
                tid = f"t_child_{i}"
                child.messages = _make_messages(
                    ("user", f"q_{i}", tid),
                    ("assistant", f"a_{i}", tid),
                )
                child.save_context()
                child_ids.append(child.session_id)
                prev_id = child.session_id
                prev_turn_id = tid

            # Resolving the deepest child triggers auto-materialize
            # on the ancestor at depth 10 in the recursion
            deepest = AgentSession.load(prev_id)
            resolved = deepest.resolve_messages()
            assert len(resolved) == 24  # 2 root + 11 * 2 child

            # The ancestor that hit depth 10 (child_ids[0], which is c0)
            # should have been materialized
            c0 = AgentSession.load(child_ids[0])
            assert c0.fork_from is None
            assert c0.fork_turn_id is None


# ---------------------------------------------------------------------------
# GC
# ---------------------------------------------------------------------------


class TestGC:
    def test_gc_deletes_expired(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            old_time = time.time() - 1_000_000
            _make_session(tmp_path, "old_session",
                          _make_messages(("user", "hi", "t1"),),
                          last_active=old_time)
            # Write the file with the old last_active directly
            ctx = json.loads((tmp_path / "old_session.context.json").read_text())
            ctx["last_active"] = old_time
            (tmp_path / "old_session.context.json").write_text(json.dumps(ctx))

            deleted = gc_sessions(max_age_seconds=100)
            assert "old_session" in deleted
            assert not (tmp_path / "old_session.context.json").exists()

    def test_gc_materializes_children_before_delete(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            old_time = time.time() - 1_000_000
            parent_msgs = _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
            )
            _make_session(tmp_path, "old_parent", parent_msgs, last_active=old_time)
            # Force old last_active
            ctx = json.loads((tmp_path / "old_parent.context.json").read_text())
            ctx["last_active"] = old_time
            (tmp_path / "old_parent.context.json").write_text(json.dumps(ctx))

            child = AgentSession.fork("old_parent", "t1")
            child.messages = _make_messages(("user", "q2", "t2"),)
            child.save_context()

            gc_sessions(max_age_seconds=100)

            # Child should be materialized (fork_from cleared)
            reloaded_child = AgentSession.load(child.session_id)
            assert reloaded_child is not None
            assert reloaded_child.fork_from is None
            assert len(reloaded_child.messages) == 3  # 2 parent + 1 child

    def test_gc_preserves_recent(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "recent",
                          _make_messages(("user", "hi", "t1"),))

            deleted = gc_sessions(max_age_seconds=604800)
            assert "recent" not in deleted
            assert AgentSession.load("recent") is not None

    def test_gc_returns_deleted_ids(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            old_time = time.time() - 1_000_000
            for sid in ["a", "b", "c"]:
                _make_session(tmp_path, sid, _make_messages(("user", "hi", "t1"),), last_active=old_time)
                ctx = json.loads((tmp_path / f"{sid}.context.json").read_text())
                ctx["last_active"] = old_time
                (tmp_path / f"{sid}.context.json").write_text(json.dumps(ctx))

            deleted = gc_sessions(max_age_seconds=100)
            assert set(deleted) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_fork_broken_chain_raises(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            # Create a child pointing to a parent that doesn't exist
            child = AgentSession(
                session_id="orphan",
                fork_from="missing_parent",
                fork_turn_id="t1",
            )
            child.save_context()

            with pytest.raises(ValueError, match="not found.*broken fork chain"):
                child.resolve_messages()

    def test_fork_at_last_turn(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent_msgs = _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
                ("user", "q2", "t2"),
                ("assistant", "a2", "t2"),
            )
            _make_session(tmp_path, "parent", parent_msgs)

            child = AgentSession.fork("parent", "t2")
            resolved = child.resolve_messages()
            # Should include all parent messages (fork at last turn = full prefix)
            assert len(resolved) == 4


# ---------------------------------------------------------------------------
# list_sessions includes fork fields
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_list_sessions_includes_fork_fields(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "parent", _make_messages(("user", "q", "t1"),))
            _make_session(
                tmp_path, "child", _make_messages(("user", "q2", "t2"),),
                fork_from="parent", fork_turn_id="t1",
            )

            sessions = list_sessions()
            child_entry = next(s for s in sessions if s["session_id"] == "child")
            parent_entry = next(s for s in sessions if s["session_id"] == "parent")

            assert child_entry["fork_from"] == "parent"
            assert child_entry["fork_turn_id"] == "t1"
            assert parent_entry["fork_from"] is None
            assert parent_entry["fork_turn_id"] is None


# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------


class TestChildren:
    def test_children_returns_child_ids(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent = _make_session(
                tmp_path, "parent",
                _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")),
            )

            c1 = AgentSession.fork("parent", "t1")
            c2 = AgentSession.fork("parent", "t1")

            children = parent.children()
            assert set(children) == {c1.session_id, c2.session_id}

    def test_children_empty_when_none(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent = _make_session(
                tmp_path, "lonely",
                _make_messages(("user", "q", "t1"),),
            )
            assert parent.children() == []


# ---------------------------------------------------------------------------
# stream() fork integration
# ---------------------------------------------------------------------------


class TestStreamForkValidation:
    """Test fork/replace_turn validation logic in stream() without mocking LLM."""

    @pytest.mark.asyncio
    async def test_stream_fork_from_nonexistent_raises(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import ALoop

            backend = ALoop(model="minimax-m2.5", api_key="test-key")
            # With fork_at provided, goes straight to AgentSession.fork()
            with pytest.raises(ValueError, match="not found"):
                async for _ in backend.stream(
                    "test", fork_from="nonexistent", fork_at="t1"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_fork_from_nonexistent_no_fork_at_raises(self, tmp_path):
        """Without fork_at, stream() loads parent to find last turn — fails."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import ALoop

            backend = ALoop(model="minimax-m2.5", api_key="test-key")
            with pytest.raises(ValueError, match="Fork source session not found"):
                async for _ in backend.stream(
                    "test", fork_from="nonexistent"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_fork_from_no_turns_raises(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import ALoop

            # Create a session with no turn_ids
            _make_session(tmp_path, "parent", [{"role": "user", "content": "hi"}])

            backend = ALoop(model="minimax-m2.5", api_key="test-key")
            with pytest.raises(ValueError, match="No turns found"):
                async for _ in backend.stream(
                    "test", fork_from="parent"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_replace_turn_without_session_raises(self):
        from aloop.agent_backend import ALoop

        backend = ALoop(model="minimax-m2.5", api_key="test-key")
        with pytest.raises(ValueError, match="replace_turn requires session_id"):
            async for _ in backend.stream(
                "test", replace_turn="t1"
            ):
                pass

    @pytest.mark.asyncio
    async def test_stream_replace_turn_nonexistent_session_raises(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import ALoop

            backend = ALoop(model="minimax-m2.5", api_key="test-key")
            with pytest.raises(ValueError, match="Session .* not found"):
                async for _ in backend.stream(
                    "test", session_id="missing", replace_turn="t1"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_replace_turn_nonexistent_turn_raises(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import ALoop

            _make_session(tmp_path, "s1", _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
            ))

            backend = ALoop(model="minimax-m2.5", api_key="test-key")
            with pytest.raises(ValueError, match="Turn .* not found"):
                async for _ in backend.stream(
                    "test", session_id="s1", replace_turn="nonexistent"
                ):
                    pass


class TestStreamForkFull:
    """Test stream() with fork_from, mocking LLM calls."""

    @pytest.mark.asyncio
    async def test_stream_fork_from(self, tmp_path):
        """stream() with fork_from + fork_at creates a child and uses parent prefix."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import ALoop
            from aloop.types import EventType

            # Create parent session with two turns
            _make_session(tmp_path, "parent", _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
                ("user", "q2", "t2"),
                ("assistant", "a2", "t2"),
            ))

            backend = ALoop(model="minimax-m2.5", api_key="test-key")

            async def mock_stream(*args, **kwargs):
                yield {"type": "text", "text": "forked reply"}
                yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

            with patch.object(backend, "_stream_completion", side_effect=mock_stream):
                events = []
                async for event in backend.stream(
                    "fork question",
                    fork_from="parent",
                    fork_at="t1",
                ):
                    events.append(event)

            # Should have gotten text events and a LOOP_END
            types = [e.type for e in events]
            assert EventType.LOOP_END in types

            # The child session should exist and have parent prefix + new messages
            loop_end = next(e for e in events if e.type == EventType.LOOP_END)
            child_sid = loop_end.data.get("session_id")
            assert child_sid is not None
            assert child_sid != "parent"

            child = AgentSession.load(child_sid)
            assert child is not None
            assert child.fork_from == "parent"
            assert child.fork_turn_id == "t1"
            # Child should have new messages (user + assistant from the stream)
            assert len(child.messages) > 0

    @pytest.mark.asyncio
    async def test_stream_fork_from_default_last_turn(self, tmp_path):
        """stream() with fork_from only (no fork_at) forks at last turn."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import ALoop
            from aloop.types import EventType

            _make_session(tmp_path, "parent", _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
                ("user", "q2", "t2"),
                ("assistant", "a2", "t2"),
            ))

            backend = ALoop(model="minimax-m2.5", api_key="test-key")

            async def mock_stream(*args, **kwargs):
                yield {"type": "text", "text": "reply"}
                yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

            with patch.object(backend, "_stream_completion", side_effect=mock_stream):
                events = []
                async for event in backend.stream(
                    "fork at last",
                    fork_from="parent",
                ):
                    events.append(event)

            loop_end = next(e for e in events if e.type == EventType.LOOP_END)
            child_sid = loop_end.data.get("session_id")
            child = AgentSession.load(child_sid)
            assert child is not None
            assert child.fork_turn_id == "t2"  # last turn

    @pytest.mark.asyncio
    async def test_stream_replace_turn(self, tmp_path):
        """stream() with replace_turn truncates messages correctly."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import ALoop
            from aloop.types import EventType

            _make_session(tmp_path, "s1", _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
                ("user", "q2", "t2"),
                ("assistant", "a2", "t2"),
                ("user", "q3", "t3"),
                ("assistant", "a3", "t3"),
            ))

            backend = ALoop(model="minimax-m2.5", api_key="test-key")

            async def mock_stream(*args, **kwargs):
                yield {"type": "text", "text": "replaced"}
                yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

            with patch.object(backend, "_stream_completion", side_effect=mock_stream):
                events = []
                async for event in backend.stream(
                    "replace q2",
                    session_id="s1",
                    replace_turn="t2",
                ):
                    events.append(event)

            types = [e.type for e in events]
            assert EventType.LOOP_END in types

            # Reload session — should only have t1 messages + the new prompt + response
            session = AgentSession.load("s1")
            assert session is not None
            # After replace: t1 messages (2) + new user msg + assistant response
            # t2 and t3 should be gone
            turn_ids = [m.get("turn_id") for m in session.messages if m.get("turn_id")]
            assert "t1" in turn_ids
            assert "t3" not in turn_ids

    @pytest.mark.asyncio
    async def test_stream_replace_first_turn(self, tmp_path):
        """replace_turn on the first turn clears all messages."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.agent_backend import ALoop
            from aloop.types import EventType

            _make_session(tmp_path, "s1", _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
                ("user", "q2", "t2"),
                ("assistant", "a2", "t2"),
            ))

            backend = ALoop(model="minimax-m2.5", api_key="test-key")

            async def mock_stream(*args, **kwargs):
                yield {"type": "text", "text": "fresh start"}
                yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

            with patch.object(backend, "_stream_completion", side_effect=mock_stream):
                events = []
                async for event in backend.stream(
                    "new beginning",
                    session_id="s1",
                    replace_turn="t1",
                ):
                    events.append(event)

            session = AgentSession.load("s1")
            assert session is not None
            # All original messages should be gone, only the new exchange remains
            old_turn_ids = {"t1", "t2"}
            remaining_turn_ids = {m.get("turn_id") for m in session.messages if m.get("turn_id")}
            assert not remaining_turn_ids.intersection(old_turn_ids)


# ---------------------------------------------------------------------------
# Fork index
# ---------------------------------------------------------------------------


class TestForkIndex:
    # --- Basics ---

    def test_fork_creates_index_entry(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "parent", _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")))
            child = AgentSession.fork("parent", "t1")

            index = _load_fork_index()
            assert index is not None
            assert child.session_id in index["parent"]

    def test_fork_multiple_children_indexed(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "parent", _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")))
            c1 = AgentSession.fork("parent", "t1")
            c2 = AgentSession.fork("parent", "t1")

            index = _load_fork_index()
            assert index is not None
            assert set(index["parent"]) == {c1.session_id, c2.session_id}

    def test_materialize_removes_index_entry(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "parent", _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")))
            child = AgentSession.fork("parent", "t1")
            child.materialize()

            index = _load_fork_index()
            assert index is not None
            assert "parent" not in index

    def test_children_uses_index(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent = _make_session(tmp_path, "parent", _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")))
            child = AgentSession.fork("parent", "t1")

            children = parent.children()
            assert children == [child.session_id]

    # --- Rebuild ---

    def test_rebuild_from_scratch(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "p", _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")))
            _make_session(tmp_path, "c1", [], fork_from="p", fork_turn_id="t1")
            _make_session(tmp_path, "c2", [], fork_from="p", fork_turn_id="t1")

            index = _rebuild_fork_index()
            assert set(index["p"]) == {"c1", "c2"}

    def test_children_rebuilds_when_index_missing(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent = _make_session(tmp_path, "parent", _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")))
            _make_session(tmp_path, "child", [], fork_from="parent", fork_turn_id="t1")

            # No index file exists
            assert not _fork_index_path().exists()

            children = parent.children()
            assert children == ["child"]
            # Index should now exist
            assert _fork_index_path().exists()

    def test_children_rebuilds_when_index_corrupt(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            parent = _make_session(tmp_path, "parent", _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")))
            _make_session(tmp_path, "child", [], fork_from="parent", fork_turn_id="t1")

            # Write garbage to index
            _fork_index_path().write_text("not json at all", encoding="utf-8")

            children = parent.children()
            assert children == ["child"]

    def test_rebuild_empty_dir(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            index = _rebuild_fork_index()
            assert index == {}

    # --- GC integration ---

    def test_gc_updates_index(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            old_time = time.time() - 1_000_000
            _make_session(tmp_path, "old_parent",
                          _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")),
                          last_active=old_time)
            # Force old last_active on disk
            ctx = json.loads((tmp_path / "old_parent.context.json").read_text())
            ctx["last_active"] = old_time
            (tmp_path / "old_parent.context.json").write_text(json.dumps(ctx))

            child = AgentSession.fork("old_parent", "t1")
            child.messages = _make_messages(("user", "q2", "t2"),)
            child.save_context()

            gc_sessions(max_age_seconds=100)

            index = _load_fork_index()
            assert index is not None
            assert "old_parent" not in index

    # --- Edge cases ---

    def test_index_unaffected_by_non_forked_sessions(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "solo1", _make_messages(("user", "q", "t1"),))
            _make_session(tmp_path, "solo2", _make_messages(("user", "q", "t1"),))

            index = _rebuild_fork_index()
            assert index == {}

    def test_materialize_skips_update_when_index_missing(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "parent", _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")))
            child = AgentSession(
                session_id="child",
                fork_from="parent",
                fork_turn_id="t1",
            )
            child.save_context()

            # No index file — materialize should not crash and should not create one
            child.materialize()
            assert not _fork_index_path().exists()

    def test_save_index_failure_silent(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            with patch("aloop.session._fork_index_path") as mock_path:
                mock_path.return_value.parent.mkdir.side_effect = OSError("disk full")
                mock_path.return_value.with_suffix.return_value.write_text.side_effect = OSError("disk full")
                # Should not raise
                _save_fork_index({"parent": ["child"]})

    # --- CLI ---

    def test_rebuild_index_cli(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "p", _make_messages(("user", "q", "t1"), ("assistant", "a", "t1")))
            _make_session(tmp_path, "c", [], fork_from="p", fork_turn_id="t1")

            from aloop.cli import _run_sessions
            from types import SimpleNamespace
            args = SimpleNamespace(sessions_action="rebuild-index")

            import io
            from unittest.mock import patch as mpatch
            with mpatch("sys.stdout", new_callable=io.StringIO) as mock_out:
                result = _run_sessions(args)

            assert result == 0
            assert "1 parent(s)" in mock_out.getvalue()
            assert "1 child(ren)" in mock_out.getvalue()
            assert _fork_index_path().exists()


# ---------------------------------------------------------------------------
# ACP fork_session integration
# ---------------------------------------------------------------------------


class TestACPForkSession:
    @pytest.mark.asyncio
    async def test_fork_session_uses_real_fork(self, tmp_path):
        """fork_session with a session that has turn_ids uses AgentSession.fork()."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.acp import AloopAgent

            agent = AloopAgent(model="minimax-m2.5")

            # Create a session with real messages
            parent = AgentSession(session_id="parent-abc")
            parent.messages = _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
            )
            parent.save_context()

            # Set up in-memory state
            from aloop.acp import _SessionState
            from aloop.agent_backend import ALoop

            backend = ALoop(model="minimax-m2.5", api_key="test")
            mem_state = _SessionState("parent-abc", str(tmp_path), backend, parent)
            agent._sessions["parent-abc"] = mem_state

            resp = await agent.fork_session(
                cwd=str(tmp_path), session_id="parent-abc"
            )

            assert resp.session_id != "parent-abc"
            assert resp.session_id in agent._sessions

            # Verify the child is a real fork
            child_state = agent._sessions[resp.session_id]
            child = child_state.agent_session
            assert child is not None
            assert child.fork_from == "parent-abc"
            assert child.fork_turn_id == "t1"

    @pytest.mark.asyncio
    async def test_fork_session_with_turn_id(self, tmp_path):
        """fork_session with explicit fork_turn_id."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.acp import AloopAgent

            agent = AloopAgent(model="minimax-m2.5")

            parent = AgentSession(session_id="parent-def")
            parent.messages = _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
                ("user", "q2", "t2"),
                ("assistant", "a2", "t2"),
            )
            parent.save_context()

            from aloop.acp import _SessionState
            from aloop.agent_backend import ALoop

            backend = ALoop(model="minimax-m2.5", api_key="test")
            mem_state = _SessionState("parent-def", str(tmp_path), backend, parent)
            agent._sessions["parent-def"] = mem_state

            resp = await agent.fork_session(
                cwd=str(tmp_path), session_id="parent-def",
                fork_turn_id="t1",
            )

            child_state = agent._sessions[resp.session_id]
            child = child_state.agent_session
            assert child.fork_turn_id == "t1"

    @pytest.mark.asyncio
    async def test_fork_session_fallback_blank(self, tmp_path):
        """fork_session falls back to blank session when source has no turn_ids."""
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.acp import AloopAgent

            agent = AloopAgent(model="minimax-m2.5")

            # Create a session with no turn_ids
            parent = AgentSession(session_id="no-turns")
            parent.messages = [{"role": "user", "content": "hi"}]
            parent.save_context()

            from aloop.acp import _SessionState
            from aloop.agent_backend import ALoop

            backend = ALoop(model="minimax-m2.5", api_key="test")
            mem_state = _SessionState("no-turns", str(tmp_path), backend, parent)
            agent._sessions["no-turns"] = mem_state

            resp = await agent.fork_session(
                cwd=str(tmp_path), session_id="no-turns"
            )

            assert resp.session_id != "no-turns"
            assert resp.session_id in agent._sessions
            # Should be a blank session, not a fork
            child_state = agent._sessions[resp.session_id]
            child = child_state.agent_session
            assert child.fork_from is None


# ---------------------------------------------------------------------------
# CLI sessions subcommand
# ---------------------------------------------------------------------------


class TestCLISessions:
    def test_parse_sessions_list(self):
        from aloop.cli import parse_args
        args = parse_args(["sessions", "list"])
        assert args.subcommand == "sessions"
        assert args.sessions_action == "list"

    def test_parse_sessions_info(self):
        from aloop.cli import parse_args
        args = parse_args(["sessions", "info", "abc123"])
        assert args.subcommand == "sessions"
        assert args.sessions_action == "info"
        assert args.session_id == "abc123"

    def test_parse_sessions_gc(self):
        from aloop.cli import parse_args
        args = parse_args(["sessions", "gc"])
        assert args.subcommand == "sessions"
        assert args.sessions_action == "gc"
        assert args.max_age == 604800

    def test_parse_sessions_gc_custom_age(self):
        from aloop.cli import parse_args
        args = parse_args(["sessions", "gc", "--max-age", "3600"])
        assert args.max_age == 3600

    def test_parse_sessions_materialize(self):
        from aloop.cli import parse_args
        args = parse_args(["sessions", "materialize", "xyz789"])
        assert args.subcommand == "sessions"
        assert args.sessions_action == "materialize"
        assert args.session_id == "xyz789"

    def test_sessions_in_subcommands(self):
        from aloop.cli import SUBCOMMANDS
        assert "sessions" in SUBCOMMANDS

    def test_sessions_list_empty(self, tmp_path, monkeypatch, capsys):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.cli import _run_sessions
            import argparse
            args = argparse.Namespace(sessions_action="list")
            result = _run_sessions(args)
            assert result == 0
            captured = capsys.readouterr()
            assert "No sessions" in captured.out

    def test_sessions_list_with_data(self, tmp_path, capsys):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "s1", _make_messages(("user", "q", "t1"),))
            _make_session(
                tmp_path, "s2", _make_messages(("user", "q2", "t2"),),
                fork_from="s1", fork_turn_id="t1",
            )

            from aloop.cli import _run_sessions
            import argparse
            args = argparse.Namespace(sessions_action="list")
            result = _run_sessions(args)
            assert result == 0
            captured = capsys.readouterr()
            assert "s1" in captured.out
            assert "s2" in captured.out
            assert "2 session(s)" in captured.out

    def test_sessions_info_found(self, tmp_path, capsys):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "myses", _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
            ))

            from aloop.cli import _run_sessions
            import argparse
            args = argparse.Namespace(sessions_action="info", session_id="myses")
            result = _run_sessions(args)
            assert result == 0
            captured = capsys.readouterr()
            assert "myses" in captured.out
            assert "Messages:" in captured.out

    def test_sessions_info_not_found(self, tmp_path, capsys):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            from aloop.cli import _run_sessions
            import argparse
            args = argparse.Namespace(sessions_action="info", session_id="nope")
            result = _run_sessions(args)
            assert result == 1

    def test_sessions_gc(self, tmp_path, capsys):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            import json as _json
            old_time = time.time() - 1_000_000
            _make_session(tmp_path, "old", _make_messages(("user", "q", "t1"),), last_active=old_time)
            ctx = _json.loads((tmp_path / "old.context.json").read_text())
            ctx["last_active"] = old_time
            (tmp_path / "old.context.json").write_text(_json.dumps(ctx))

            from aloop.cli import _run_sessions
            import argparse
            args = argparse.Namespace(sessions_action="gc", max_age=100)
            result = _run_sessions(args)
            assert result == 0
            captured = capsys.readouterr()
            assert "old" in captured.out

    def test_sessions_materialize(self, tmp_path, capsys):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "parent", _make_messages(
                ("user", "q1", "t1"),
                ("assistant", "a1", "t1"),
            ))
            child = AgentSession.fork("parent", "t1")
            child.messages = _make_messages(("user", "q2", "t2"),)
            child.save_context()

            from aloop.cli import _run_sessions
            import argparse
            args = argparse.Namespace(sessions_action="materialize", session_id=child.session_id)
            result = _run_sessions(args)
            assert result == 0

            # Verify materialization
            reloaded = AgentSession.load(child.session_id)
            assert reloaded.fork_from is None
            assert len(reloaded.messages) == 3

    def test_sessions_materialize_non_forked(self, tmp_path, capsys):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            _make_session(tmp_path, "plain", _make_messages(("user", "q", "t1"),))

            from aloop.cli import _run_sessions
            import argparse
            args = argparse.Namespace(sessions_action="materialize", session_id="plain")
            result = _run_sessions(args)
            assert result == 0
            captured = capsys.readouterr()
            assert "not forked" in captured.out
