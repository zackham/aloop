"""Tests for AgentSession.spawn_metadata field roundtrip."""

from __future__ import annotations

import json
from unittest.mock import patch

from aloop.session import AgentSession


class TestSessionSpawnMetadata:
    def test_default_spawn_metadata_none(self):
        s = AgentSession(session_id="abc")
        assert s.spawn_metadata is None

    def test_save_load_with_spawn_metadata(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            s = AgentSession(
                session_id="child1",
                spawn_metadata={
                    "kind": "fork",
                    "parent_session_id": "parent1",
                    "parent_turn_id": "t123",
                    "spawning_mode": "orchestrator",
                    "child_mode": None,
                    "timestamp": 1234567890.0,
                },
            )
            s.save_context()

            loaded = AgentSession.load("child1")
            assert loaded is not None
            assert loaded.spawn_metadata is not None
            assert loaded.spawn_metadata["kind"] == "fork"
            assert loaded.spawn_metadata["parent_session_id"] == "parent1"
            assert loaded.spawn_metadata["spawning_mode"] == "orchestrator"

    def test_save_load_without_spawn_metadata(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            s = AgentSession(session_id="normal")
            s.save_context()

            loaded = AgentSession.load("normal")
            assert loaded is not None
            assert loaded.spawn_metadata is None

    def test_existing_session_files_load_without_spawn_metadata(self, tmp_path):
        # Synthesize an old context.json without the spawn_metadata key
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            tmp_path.mkdir(parents=True, exist_ok=True)
            ctx_file = tmp_path / "old_session.context.json"
            ctx_file.write_text(
                json.dumps(
                    {
                        "session_id": "old_session",
                        "messages": [],
                        "last_compaction": None,
                        "created_at": 100.0,
                        "last_active": 200.0,
                        "fork_from": None,
                        "fork_turn_id": None,
                    }
                )
            )

            loaded = AgentSession.load("old_session")
            assert loaded is not None
            assert loaded.spawn_metadata is None

    def test_spawn_metadata_persisted_in_context_json_payload(self, tmp_path):
        with patch("aloop.session._sessions_dir", return_value=tmp_path):
            s = AgentSession(
                session_id="payload_test",
                spawn_metadata={"kind": "fresh", "child_mode": "reviewer"},
            )
            s.save_context()

            data = json.loads(
                (tmp_path / "payload_test.context.json").read_text()
            )
            assert "spawn_metadata" in data
            assert data["spawn_metadata"]["kind"] == "fresh"
            assert data["spawn_metadata"]["child_mode"] == "reviewer"
