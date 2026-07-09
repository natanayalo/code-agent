"""Deterministic shaping of gated project memories for worker context."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from orchestrator.state import (
    MemoryEntry,
    RepositoryMemoryProfile,
    RepositoryMemoryProfileItem,
    RepositoryProfileSection,
)

_SECTION_ORDER: tuple[RepositoryProfileSection, ...] = (
    "verification_commands",
    "conventions",
    "pitfalls",
    "remembered_instructions",
    "general_facts",
)
_KEY_TO_SECTION: dict[str, RepositoryProfileSection] = {
    "verification_commands": "verification_commands",
    "verification_command": "verification_commands",
    "test_commands": "verification_commands",
    "test_command": "verification_commands",
    "repo_convention": "conventions",
    "convention": "conventions",
    "known_pitfalls": "pitfalls",
    "known_pitfall": "pitfalls",
    "remembered_instruction": "remembered_instructions",
    "remembered_instructions": "remembered_instructions",
}


def _sort_timestamp(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _section_for_key(memory_key: str) -> RepositoryProfileSection:
    return _KEY_TO_SECTION.get(memory_key.strip().lower(), "general_facts")


def _profile_item(
    entry: MemoryEntry,
    section: RepositoryProfileSection,
) -> RepositoryMemoryProfileItem:
    return RepositoryMemoryProfileItem(
        memory_key=entry.memory_key,
        value=dict(entry.value),
        section=section,
        source=entry.source,
        confidence=entry.confidence,
        last_verified_at=entry.last_verified_at,
        requires_verification=entry.requires_verification,
        gate_status=entry.gate_status,
        gate_reason_codes=list(entry.gate_reason_codes),
        advisory_strength=entry.advisory_strength,
        risk=entry.risk,
        conflict=entry.conflict,
    )


def _sort_items(items: list[RepositoryMemoryProfileItem]) -> list[RepositoryMemoryProfileItem]:
    max_datetime = datetime.max.replace(tzinfo=UTC)
    return sorted(
        items,
        key=lambda item: (
            0 if item.gate_status == "accepted" else 1,
            -item.advisory_strength,
            max_datetime - _sort_timestamp(item.last_verified_at),
            item.memory_key,
        ),
    )


def shape_repository_memory_profile(
    project_memories: list[MemoryEntry],
) -> RepositoryMemoryProfile:
    """Shape gated project memories without changing their policy meaning."""
    grouped: dict[RepositoryProfileSection, list[RepositoryMemoryProfileItem]] = {
        section: [] for section in _SECTION_ORDER
    }
    for entry in project_memories:
        if entry.gate_status not in {"accepted", "advisory"}:
            continue
        section = _section_for_key(entry.memory_key)
        grouped[section].append(_profile_item(entry, section))

    payload = {section: _sort_items(grouped[section]) for section in _SECTION_ORDER}
    return RepositoryMemoryProfile.model_validate(payload)


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, dict) else {}
    legacy_dict = getattr(value, "dict", None)
    if callable(legacy_dict):
        dumped = legacy_dict()
        return dict(dumped) if isinstance(dumped, dict) else {}
    return {}


def _profile_items(profile: Any, section: str) -> list[Any]:
    raw_items = _to_dict(profile).get(section)
    return raw_items if isinstance(raw_items, list) else []


def profile_counts(profile: RepositoryMemoryProfile | dict[str, Any] | None) -> dict[str, int]:
    """Return stable section counts for memory-loaded diagnostics."""
    if profile is None:
        return {section: 0 for section in _SECTION_ORDER}
    return {section: len(_profile_items(profile, section)) for section in _SECTION_ORDER}


def profile_source_keys(profile: RepositoryMemoryProfile | dict[str, Any] | None) -> list[str]:
    """Return stable source memory keys represented in the profile."""
    if profile is None:
        return []
    keys: list[str] = []
    for section in _SECTION_ORDER:
        for item in _profile_items(profile, section):
            memory_key = _to_dict(item).get("memory_key")
            if isinstance(memory_key, str):
                keys.append(memory_key)
    return keys


__all__ = [
    "profile_counts",
    "profile_source_keys",
    "shape_repository_memory_profile",
]
