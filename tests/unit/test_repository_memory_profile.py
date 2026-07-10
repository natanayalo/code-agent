"""Tests for deterministic repository memory profile shaping."""

from datetime import UTC, datetime, timedelta

from memory.repository_profile import (
    profile_counts,
    profile_source_keys,
    shape_repository_memory_profile,
)
from orchestrator.graph import _apply_read_side_gate
from orchestrator.state import MemoryContext, MemoryEntry


def _entry(key: str, *, status: str = "accepted", strength: float = 1.0) -> MemoryEntry:
    return MemoryEntry(
        memory_key=key,
        value={"value": key},
        source="test",
        last_verified_at=datetime.now(UTC),
        gate_status=status,
        advisory_strength=strength,
        gate_reason_codes=["requires_verification"] if status == "advisory" else [],
    )


def test_profile_groups_canonical_and_legacy_keys_and_preserves_metadata() -> None:
    profile = shape_repository_memory_profile(
        [
            _entry("known_pitfall"),
            _entry("test_command", status="advisory", strength=0.4),
            _entry("repo_convention"),
            _entry("remembered_instruction"),
            _entry("custom_fact"),
            _entry("suppressed", status="suppressed"),
        ]
    )

    assert [item.memory_key for item in profile.verification_commands] == ["test_command"]
    assert [item.memory_key for item in profile.pitfalls] == ["known_pitfall"]
    assert [item.memory_key for item in profile.conventions] == ["repo_convention"]
    assert [item.memory_key for item in profile.remembered_instructions] == [
        "remembered_instruction"
    ]
    assert [item.memory_key for item in profile.general_facts] == ["custom_fact"]
    assert profile.verification_commands[0].gate_reason_codes == ["requires_verification"]
    assert profile.verification_commands[0].requires_verification is True
    assert profile.verification_commands[0].risk == "low"
    assert "suppressed" not in profile_source_keys(profile)


def test_profile_sorts_accepted_then_strength_recency_and_key() -> None:
    now = datetime.now(UTC)
    entries = [
        _entry("zeta", strength=0.9),
        _entry("alpha", strength=0.9),
        _entry("advisory", status="advisory", strength=1.0),
    ]
    entries[0].last_verified_at = now - timedelta(days=1)
    entries[1].last_verified_at = now

    profile = shape_repository_memory_profile(entries)

    assert [item.memory_key for item in profile.general_facts] == ["alpha", "zeta", "advisory"]


def test_profile_diagnostics_are_stable_for_empty_input() -> None:
    profile = shape_repository_memory_profile([])

    assert profile_counts(profile) == {
        "verification_commands": 0,
        "conventions": 0,
        "pitfalls": 0,
        "remembered_instructions": 0,
        "general_facts": 0,
    }
    assert profile_source_keys(profile) == []


def test_profile_diagnostics_accept_serialized_profile_dicts() -> None:
    profile = {
        "verification_commands": [
            {"memory_key": "test_command"},
        ],
        "conventions": None,
    }

    assert profile_counts(profile)["verification_commands"] == 1
    assert profile_source_keys(profile) == ["test_command"]


def test_read_side_gate_excludes_suppressed_project_entries_from_profile() -> None:
    memory = _apply_read_side_gate(
        MemoryContext(
            project=[
                _entry("safe_convention"),
                _entry("deploy_approval", status="accepted"),
            ]
        )
    )

    # The high-risk key is unverified by default and must be suppressed.
    keys = profile_source_keys(memory.repository_profile)
    assert "safe_convention" in keys
    assert "deploy_approval" not in keys


def test_profile_groups_new_key_variations() -> None:
    profile = shape_repository_memory_profile(
        [
            _entry("conventions"),
            _entry("pitfalls"),
            _entry("instructions"),
        ]
    )
    assert [item.memory_key for item in profile.conventions] == ["conventions"]
    assert [item.memory_key for item in profile.pitfalls] == ["pitfalls"]
    assert [item.memory_key for item in profile.remembered_instructions] == ["instructions"]


def test_to_dict_defensive_handling() -> None:
    class FailingDump:
        def model_dump(self) -> None:
            raise ValueError("model_dump failed")

        def dict(self) -> None:
            raise ValueError("dict failed")

    from utils.serialization import to_dict

    assert to_dict(FailingDump()) == {}


def test_profile_sort_items_handles_none_advisory_strength() -> None:
    from memory.repository_profile import _sort_items
    from orchestrator.state import RepositoryMemoryProfileItem

    item = RepositoryMemoryProfileItem.model_construct(
        memory_key="custom_fact",
        gate_status="accepted",
        advisory_strength=None,
        last_verified_at=None,
    )
    sorted_items = _sort_items([item])
    assert sorted_items[0].memory_key == "custom_fact"
