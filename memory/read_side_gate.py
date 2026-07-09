"""Read-side memory gate service to score and filter loaded memory entries."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from orchestrator.state import MemoryEntry

# We define high risk keywords for exact token match
HIGH_RISK_KEYWORDS = {
    "auth",
    "token",
    "password",
    "secret",
    "credential",
    "api_key",
    "private_key",
    "access_key",
    "deploy",
    "deployment",
    "migration",
    "production",
    "prod",
    "delete",
    "drop",
    "permission",
    "approval",
}

_HIGH_RISK_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(k.replace("_", " ")) for k in HIGH_RISK_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

COLLECTION_KEYS = {
    "verification_commands",
    "known_pitfalls",
    "repo_convention",
    "remembered_instruction",
}


class MemoryGateDecision(BaseModel):
    """Details of a suppressed memory entry on the read-side."""

    memory_key: str
    category: str
    reason_codes: list[str] = Field(default_factory=list)
    value: dict[str, Any] = Field(default_factory=dict)


class ReadSideMemoryGateResult(BaseModel):
    """The result of the read-side memory gate process."""

    accepted_personal: list[MemoryEntry] = Field(default_factory=list)
    accepted_project: list[MemoryEntry] = Field(default_factory=list)
    suppressed_personal: list[MemoryGateDecision] = Field(default_factory=list)
    suppressed_project: list[MemoryGateDecision] = Field(default_factory=list)
    reason_counts: dict[str, int] = Field(default_factory=dict)


def _resolve_staleness_window_days(memory_key: str, risk: str) -> int:
    """Resolve staleness window in days based on key type and risk."""
    key_lower = memory_key.lower()
    if any(k in key_lower for k in ["verification_commands", "verification"]):
        return 30
    if "repo_convention" in key_lower:
        return 90
    if "known_pitfalls" in key_lower:
        return 60
    if risk == "high" or any(k in key_lower for k in ["approval", "deploy", "security"]):
        return 14
    if "preference" in key_lower or "communication" in key_lower:
        return 180
    return 30


def _calculate_staleness(
    last_verified_at: datetime | None,
    requires_verification: bool,
    window_days: int,
) -> float:
    """Calculate staleness score between 0.0 and 1.0."""
    if last_verified_at is None:
        return 1.0 if requires_verification else 0.0
    if window_days <= 0:
        return 1.0
    now = datetime.now(UTC)
    if last_verified_at.tzinfo is None:
        verified_utc = last_verified_at.replace(tzinfo=UTC)
    else:
        verified_utc = last_verified_at.astimezone(UTC)
    delta_days = (now - verified_utc).total_seconds() / (3600.0 * 24.0)
    return min(1.0, max(0.0, delta_days / window_days))


def _determine_risk(
    memory_key: str,
    requires_verification: bool,
) -> Literal["low", "medium", "high"]:
    """Determine risk level of memory entry."""
    normalized = re.sub(r"[^a-zA-Z0-9]", " ", memory_key)
    normalized = re.sub(r"([a-z])([A-Z])", r"\1 \2", normalized)
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", normalized)
    normalized = normalized.lower()

    if _HIGH_RISK_PATTERN.search(normalized):
        return "high"

    if requires_verification:
        return "medium"
    return "low"


def _values_contradict(val1: Any, val2: Any) -> bool:
    """Recursively check if two values contradict directly."""
    if type(val1) is not type(val2):
        return True
    if isinstance(val1, dict):
        for k, v in val1.items():
            if k in val2 and _values_contradict(v, val2[k]):
                return True
        return False
    if isinstance(val1, list):
        return False
    return val1 != val2


def _confidence_value(value: float | None) -> float:
    return 1.0 if value is None else value


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _deduplicate_by_key(entries: list[MemoryEntry]) -> list[MemoryEntry]:
    deduped: dict[str, MemoryEntry] = {}
    for entry in entries:
        key = entry.memory_key
        if key not in deduped:
            deduped[key] = entry
        else:
            existing = deduped[key]
            e_time = _as_utc(entry.last_verified_at)
            ex_time = _as_utc(existing.last_verified_at)
            if e_time > ex_time or (
                e_time == ex_time
                and _confidence_value(entry.confidence) > _confidence_value(existing.confidence)
            ):
                deduped[key] = entry
    return list(deduped.values())


class ReadSideMemoryGateService:
    """Gating logic for analyzing memory relevance, staleness, conflicts, and risk."""

    @classmethod
    def process(
        cls,
        *,
        personal: list[MemoryEntry],
        project: list[MemoryEntry],
    ) -> ReadSideMemoryGateResult:
        """Process memories, annotate metadata, handle conflicts, and split accepted/suppressed."""
        annotated_personal = [cls._annotate(e) for e in personal]
        annotated_project = [cls._annotate(e) for e in project]

        # Deduplicate within scopes before conflict resolution
        deduped_personal = _deduplicate_by_key(annotated_personal)
        deduped_project = _deduplicate_by_key(annotated_project)

        return cls._resolve_conflicts(deduped_personal, deduped_project)

    @classmethod
    def _annotate(cls, entry: MemoryEntry) -> MemoryEntry:
        """Calculate and attach gating metadata fields on a copy of MemoryEntry."""
        risk = _determine_risk(entry.memory_key, entry.requires_verification)
        window = _resolve_staleness_window_days(entry.memory_key, risk)
        staleness = _calculate_staleness(
            entry.last_verified_at,
            entry.requires_verification,
            window,
        )

        confidence_val = entry.confidence if entry.confidence is not None else 1.0
        strength = confidence_val * (1.0 - staleness * 0.5)
        reason_codes = []

        if entry.requires_verification:
            strength *= 0.7
            reason_codes.append("requires_verification_penalty")

        strength = min(1.0, max(0.0, strength))

        status = "accepted"
        if risk == "high" and (entry.requires_verification or staleness >= 0.5):
            status = "suppressed"
            reason_codes.append("high_risk_unverified_or_stale")
        elif risk == "medium" or entry.requires_verification or staleness >= 0.5:
            status = "advisory"
            reason_codes.append("medium_risk_or_stale")

        return entry.model_copy(
            update={
                "risk": risk,
                "staleness": staleness,
                "advisory_strength": strength,
                "gate_status": status,
                "gate_reason_codes": reason_codes,
            }
        )

    @classmethod
    def _resolve_conflicts(
        cls,
        personal: list[MemoryEntry],
        project: list[MemoryEntry],
    ) -> ReadSideMemoryGateResult:
        """Resolve conflicts and compute final accepted and suppressed sets."""
        accepted_personal: list[MemoryEntry] = []
        accepted_project: list[MemoryEntry] = []
        suppressed_personal: list[MemoryGateDecision] = []
        suppressed_project: list[MemoryGateDecision] = []
        reason_counts: dict[str, int] = {}

        project_by_key: dict[str, MemoryEntry] = {}
        for entry in project:
            if entry.gate_status == "suppressed":
                suppressed_project.append(
                    MemoryGateDecision(
                        memory_key=entry.memory_key,
                        category="project",
                        reason_codes=entry.gate_reason_codes,
                        value=entry.value,
                    )
                )
                for rc in entry.gate_reason_codes:
                    reason_counts[rc] = reason_counts.get(rc, 0) + 1
            else:
                accepted_project.append(entry)
                project_by_key[entry.memory_key] = entry

        for entry in personal:
            if entry.gate_status == "suppressed":
                suppressed_personal.append(
                    MemoryGateDecision(
                        memory_key=entry.memory_key,
                        category="personal",
                        reason_codes=entry.gate_reason_codes,
                        value=entry.value,
                    )
                )
                for rc in entry.gate_reason_codes:
                    reason_counts[rc] = reason_counts.get(rc, 0) + 1
                continue

            proj_match = project_by_key.get(entry.memory_key)
            if proj_match is not None:
                cls._handle_matching_key_conflict(
                    entry,
                    proj_match,
                    accepted_personal,
                    suppressed_personal,
                    reason_counts,
                )
            else:
                accepted_personal.append(entry)

        return ReadSideMemoryGateResult(
            accepted_personal=accepted_personal,
            accepted_project=accepted_project,
            suppressed_personal=suppressed_personal,
            suppressed_project=suppressed_project,
            reason_counts=reason_counts,
        )

    @classmethod
    def _handle_matching_key_conflict(
        cls,
        personal_entry: MemoryEntry,
        proj_match: MemoryEntry,
        accepted_personal: list[MemoryEntry],
        suppressed_personal: list[MemoryGateDecision],
        reason_counts: dict[str, int],
    ) -> None:
        """Resolve same-key personal vs project conflict and update lists/reason counts."""
        key = personal_entry.memory_key
        is_collection = key in COLLECTION_KEYS

        if not is_collection:
            reason = "project_overrides_personal"
            reason_codes = [reason]
            if _values_contradict(proj_match.value, personal_entry.value):
                proj_match.conflict = "personal_conflict_resolved"
                proj_match.advisory_strength = min(
                    1.0, max(0.0, proj_match.advisory_strength * 0.8)
                )
                if "resolved_conflict_penalty" not in proj_match.gate_reason_codes:
                    proj_match.gate_reason_codes.append("resolved_conflict_penalty")

            suppressed_personal.append(
                MemoryGateDecision(
                    memory_key=key,
                    category="personal",
                    reason_codes=reason_codes,
                    value=personal_entry.value,
                )
            )
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        else:
            if _values_contradict(proj_match.value, personal_entry.value):
                reason = "high_risk_conflict"
                suppressed_personal.append(
                    MemoryGateDecision(
                        memory_key=key,
                        category="personal",
                        reason_codes=[reason],
                        value=personal_entry.value,
                    )
                )
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                proj_match.conflict = "personal_conflict_resolved"
                proj_match.advisory_strength = min(
                    1.0, max(0.0, proj_match.advisory_strength * 0.8)
                )
                if "resolved_conflict_penalty" not in proj_match.gate_reason_codes:
                    proj_match.gate_reason_codes.append("resolved_conflict_penalty")
            else:
                accepted_personal.append(personal_entry)
