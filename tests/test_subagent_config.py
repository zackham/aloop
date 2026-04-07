"""Tests for validate_subagent_config()."""

from __future__ import annotations

from aloop.config import validate_subagent_config


class TestValidateSubagentConfig:
    def test_empty_config_no_errors(self):
        assert validate_subagent_config({}) == []

    def test_no_modes_no_errors(self):
        assert validate_subagent_config({"other": "key"}) == []

    def test_modes_not_dict_no_errors(self):
        assert validate_subagent_config({"modes": "wrong"}) == []

    def test_subagent_eligible_must_be_bool(self):
        config = {
            "modes": {
                "explore": {"subagent_eligible": "yes"},
            }
        }
        errors = validate_subagent_config(config)
        assert any("subagent_eligible must be a bool" in e for e in errors)

    def test_can_fork_must_be_bool(self):
        config = {
            "modes": {
                "orchestrator": {"can_fork": 1},
            }
        }
        errors = validate_subagent_config(config)
        assert any("can_fork must be a bool" in e for e in errors)

    def test_spawnable_modes_must_be_list(self):
        config = {
            "modes": {
                "orchestrator": {"spawnable_modes": "not_a_list"},
            }
        }
        errors = validate_subagent_config(config)
        assert any("must be a list of strings" in e for e in errors)

    def test_spawnable_modes_must_be_strings(self):
        config = {
            "modes": {
                "orchestrator": {"spawnable_modes": ["explore", 42]},
            }
        }
        errors = validate_subagent_config(config)
        assert any("must be a list of strings" in e for e in errors)

    def test_spawnable_modes_references_unknown_mode_errors(self):
        config = {
            "modes": {
                "orchestrator": {"spawnable_modes": ["nonexistent"]},
            }
        }
        errors = validate_subagent_config(config)
        assert any("unknown mode 'nonexistent'" in e for e in errors)

    def test_spawnable_modes_references_non_eligible_mode_errors(self):
        config = {
            "modes": {
                "orchestrator": {"spawnable_modes": ["explore"]},
                "explore": {},  # missing subagent_eligible: True
            }
        }
        errors = validate_subagent_config(config)
        assert any("not subagent_eligible" in e for e in errors)

    def test_valid_subagent_config_no_errors(self):
        config = {
            "modes": {
                "orchestrator": {
                    "can_fork": True,
                    "spawnable_modes": ["explore", "reviewer"],
                },
                "explore": {"subagent_eligible": True},
                "reviewer": {"subagent_eligible": True},
            }
        }
        assert validate_subagent_config(config) == []

    def test_self_referential_spawnable_modes_ok(self):
        # explore can spawn another explore (recursive subagent pattern)
        config = {
            "modes": {
                "explore": {
                    "subagent_eligible": True,
                    "spawnable_modes": ["explore"],
                },
            }
        }
        assert validate_subagent_config(config) == []

    def test_terminal_mode_no_spawnable_ok(self):
        # A mode that has subagent_eligible but no spawnable_modes is valid
        config = {
            "modes": {
                "leaf": {"subagent_eligible": True},
            }
        }
        assert validate_subagent_config(config) == []

    def test_mode_eligible_but_not_listed_anywhere_ok(self):
        # eligible modes are valid even if no other mode lists them
        config = {
            "modes": {
                "explore": {"subagent_eligible": True},
                "worker": {},
            }
        }
        assert validate_subagent_config(config) == []

    def test_multiple_errors_collected(self):
        config = {
            "modes": {
                "bad_one": {"subagent_eligible": "yes"},
                "bad_two": {"can_fork": "no"},
                "bad_three": {"spawnable_modes": ["nonexistent"]},
            }
        }
        errors = validate_subagent_config(config)
        # At least three errors should be collected
        assert len(errors) >= 3
