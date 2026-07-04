"""Memory admission boundary and deterministic policy."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from db.enums import MemoryProposalCategory
from db.models import ProjectMemory
from repositories.sqlalchemy_memory import PersonalMemoryRepository, ProjectMemoryRepository
from repositories.sqlalchemy_memory_admission import MemoryAdmissionDecisionRepository
from repositories.sqlalchemy_memory_proposal import MemoryProposalRepository

MemoryAdmissionDecision = Literal["reject", "create", "update", "merge", "needs_human_review"]
MemoryRiskLevel = Literal["low", "medium", "high", "blocked"]
MemoryProducer = Literal["worker", "operator", "system", "import"]

_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_]{12,}\b"),
)
_SECRET_KEY_PATTERN = re.compile(
    r"(?<![a-zA-Z0-9])(?:api[_-]?(?:key|token)|secret|token|password|credential)s?(?![a-zA-Z0-9])",
    re.IGNORECASE,
)
_PLACEHOLDER_SECRET_VALUES = {
    "",
    "***",
    "****",
    "<redacted>",
    "redacted",
    "[redacted]",
    "removed",
    "unset",
}
_SPECULATIVE_PATTERNS = (
    re.compile(r"\b(?:maybe|might|probably|possibly|I think|not sure|seems like)\b", re.I),
)
_HUMAN_REVIEW_KEYWORDS = (
    "approval",
    "communication",
    "convention",
    "pitfall",
    "preference",
    "style",
    "workflow",
)


class MemoryAdmissionModel(BaseModel):
    """Base model for memory-admission DTOs."""

    model_config = ConfigDict(extra="forbid")


class MemoryCandidate(MemoryAdmissionModel):
    """Candidate memory produced by a worker or operator before admission."""

    category: Literal["personal", "project"]
    memory_key: str
    value: dict[str, Any] = Field(default_factory=dict)
    repo_url: str | None = None
    source: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    scope: str | None = None
    last_verified_at: datetime | None = None
    requires_verification: bool = True
    evidence: list[str] = Field(default_factory=list)
    task_id: str | None = None
    session_id: str | None = None
    producer: MemoryProducer = "worker"


class MemoryAdmissionResult(MemoryAdmissionModel):
    """Admission decision for a single candidate."""

    candidate: MemoryCandidate
    decision: MemoryAdmissionDecision
    risk_level: MemoryRiskLevel
    reason: str
    durable_memory_id: str | None = None
    proposal_id: str | None = None


MemoryAdmissionCandidate = MemoryCandidate


class MemoryAdmissionBatchResult(MemoryAdmissionModel):
    """Admission result for a batch of candidates."""

    results: list[MemoryAdmissionResult] = Field(default_factory=list)

    @property
    def decision_counts(self) -> dict[str, int]:
        """Return decision totals for timeline payloads and logs."""
        return dict(Counter(result.decision for result in self.results))

    @property
    def risk_counts(self) -> dict[str, int]:
        """Return risk totals for timeline payloads and logs."""
        return dict(Counter(result.risk_level for result in self.results))

    @property
    def durable_write_count(self) -> int:
        """Return the count of candidates written directly to durable memory."""
        return sum(1 for result in self.results if result.durable_memory_id is not None)

    @property
    def proposal_count(self) -> int:
        """Return the count of candidates routed to human review."""
        return sum(1 for result in self.results if result.proposal_id is not None)

    @property
    def rejected_count(self) -> int:
        """Return the count of rejected candidates."""
        return sum(1 for result in self.results if result.decision == "reject")


class MemoryAdmissionService(ABC):
    """Boundary for classifying candidate memories before durable writes."""

    @abstractmethod
    def admit_candidates(
        self,
        *,
        candidates: list[MemoryCandidate],
    ) -> MemoryAdmissionBatchResult:
        """Classify candidates and apply the resulting admission decisions."""


class CustomMemoryAdmissionService(MemoryAdmissionService):
    """Deterministic custom baseline for M23 Slice 5 admission."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def admit_candidates(
        self,
        *,
        candidates: list[MemoryCandidate],
    ) -> MemoryAdmissionBatchResult:
        """Apply deterministic admission policy to candidate memories."""
        results = [self._admit_candidate(candidate) for candidate in candidates]
        return MemoryAdmissionBatchResult(results=results)

    def _admit_candidate(self, candidate: MemoryCandidate) -> MemoryAdmissionResult:
        decision, risk_level, reason = self._classify(candidate)
        durable_memory_id: str | None = None
        proposal_id: str | None = None

        if decision in {"create", "update", "merge"}:
            durable_memory_id = self._write_durable_memory(candidate, decision)
        elif decision == "needs_human_review":
            proposal_id = self._create_proposal(candidate, risk_level, reason)

        result = MemoryAdmissionResult(
            candidate=candidate,
            decision=decision,
            risk_level=risk_level,
            reason=reason,
            durable_memory_id=durable_memory_id,
            proposal_id=proposal_id,
        )
        self._record_decision(result)
        return result

    def _classify(
        self,
        candidate: MemoryCandidate,
    ) -> tuple[MemoryAdmissionDecision, MemoryRiskLevel, str]:
        if not candidate.memory_key.strip():
            return "reject", "blocked", "memory_key is required."
        if not candidate.value:
            return "reject", "blocked", "candidate value is empty."
        if candidate.category == "project" and not candidate.repo_url:
            return "reject", "blocked", "project memory requires repo_url."
        if _contains_secret(candidate):
            return "reject", "blocked", "candidate appears to contain a secret."
        if _is_speculative(candidate):
            return "reject", "high", "candidate is speculative or uncertain."
        if candidate.category == "personal":
            return "needs_human_review", "medium", "personal memory requires human review."

        existing = self._get_existing_project_memory(candidate)
        if _requires_human_review(candidate):
            return "needs_human_review", "medium", "candidate category requires human review."
        if not candidate.evidence:
            return "needs_human_review", "medium", "direct write requires evidence."
        if candidate.confidence < 0.85:
            return "needs_human_review", "medium", "direct write requires high confidence."
        if existing is None:
            return "create", "low", "low-risk evidenced project memory can be created."
        existing_value = existing.value if isinstance(existing.value, dict) else {}
        if _has_conflicting_values(existing_value, candidate.value):
            return "needs_human_review", "medium", "candidate conflicts with existing memory."
        if _can_merge(existing_value, candidate.value):
            return "merge", "low", "non-conflicting object values can be merged."
        return "update", "low", "candidate supersedes existing memory without conflict."

    def _write_durable_memory(
        self,
        candidate: MemoryCandidate,
        decision: MemoryAdmissionDecision,
    ) -> str:
        if candidate.category == "personal":
            personal_memory = PersonalMemoryRepository(self.session).upsert(
                memory_key=candidate.memory_key.strip(),
                value=dict(candidate.value),
                source=candidate.source,
                confidence=candidate.confidence,
                scope=candidate.scope,
                last_verified_at=candidate.last_verified_at,
                requires_verification=candidate.requires_verification,
            )
            return personal_memory.id

        if not candidate.repo_url:
            raise ValueError("Project memory requires repo_url.")
        value = dict(candidate.value)
        existing = ProjectMemoryRepository(self.session).get(
            repo_url=candidate.repo_url,
            memory_key=candidate.memory_key.strip(),
        )
        if decision == "merge" and existing is not None and isinstance(existing.value, dict):
            value = {**existing.value, **value}
        project_memory = ProjectMemoryRepository(self.session).upsert(
            repo_url=candidate.repo_url,
            memory_key=candidate.memory_key.strip(),
            value=value,
            source=candidate.source,
            confidence=candidate.confidence,
            scope=candidate.scope,
            last_verified_at=candidate.last_verified_at,
            requires_verification=candidate.requires_verification,
        )
        return project_memory.id

    def _create_proposal(
        self,
        candidate: MemoryCandidate,
        risk_level: MemoryRiskLevel,
        reason: str,
    ) -> str:
        proposal = MemoryProposalRepository(self.session).create(
            category=MemoryProposalCategory(candidate.category),
            repo_url=candidate.repo_url,
            memory_key=candidate.memory_key.strip(),
            value=dict(candidate.value),
            source=candidate.source,
            confidence=candidate.confidence,
            scope=candidate.scope,
            requires_verification=candidate.requires_verification,
            title=f"Review memory: {candidate.memory_key.strip()}",
            summary=reason,
            evidence={
                "admission_reason": reason,
                "risk_level": risk_level,
                "evidence": list(candidate.evidence),
                "producer": candidate.producer,
            },
            task_id=candidate.task_id,
            session_id=candidate.session_id,
        )
        return proposal.id

    def _record_decision(self, result: MemoryAdmissionResult) -> None:
        MemoryAdmissionDecisionRepository(self.session).create(
            category=result.candidate.category,
            memory_key=result.candidate.memory_key.strip(),
            candidate_payload=result.candidate.model_dump(mode="json"),
            decision=result.decision,
            risk_level=result.risk_level,
            reason=result.reason,
            task_id=result.candidate.task_id,
            session_id=result.candidate.session_id,
            durable_memory_id=result.durable_memory_id,
            proposal_id=result.proposal_id,
        )

    def _get_existing_project_memory(self, candidate: MemoryCandidate) -> ProjectMemory | None:
        if candidate.category != "project" or not candidate.repo_url:
            return None
        return ProjectMemoryRepository(self.session).get(
            repo_url=candidate.repo_url,
            memory_key=candidate.memory_key.strip(),
        )


def _candidate_text(candidate: MemoryCandidate) -> str:
    return f"{candidate.memory_key} {candidate.value} {candidate.source or ''}"


def _contains_secret(candidate: MemoryCandidate) -> bool:
    if _SECRET_KEY_PATTERN.search(candidate.memory_key.strip()):
        return True
    if _contains_secret_value(candidate.value):
        return True
    return _contains_secret_key_with_value(candidate.value)


def _contains_secret_value(value: Any) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)
    if isinstance(value, dict):
        return any(_contains_secret_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_secret_value(item) for item in value)
    return False


def _contains_secret_key_with_value(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (_SECRET_KEY_PATTERN.search(str(key).strip()) and _has_non_placeholder_value(item))
            or _contains_secret_key_with_value(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_key_with_value(item) for item in value)
    return False


def _has_non_placeholder_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() not in _PLACEHOLDER_SECRET_VALUES
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return True
    if isinstance(value, dict):
        return any(_has_non_placeholder_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_non_placeholder_value(item) for item in value)
    return value is not None


def _is_speculative(candidate: MemoryCandidate) -> bool:
    text = _candidate_text(candidate)
    return any(pattern.search(text) for pattern in _SPECULATIVE_PATTERNS)


def _requires_human_review(candidate: MemoryCandidate) -> bool:
    key_and_scope = f"{candidate.memory_key} {candidate.scope or ''}".casefold()
    return any(keyword in key_and_scope for keyword in _HUMAN_REVIEW_KEYWORDS)


def _has_conflicting_values(
    existing_value: dict[str, Any], candidate_value: dict[str, Any]
) -> bool:
    return any(
        key in existing_value and existing_value[key] != value
        for key, value in candidate_value.items()
    )


def _can_merge(existing_value: dict[str, Any], candidate_value: dict[str, Any]) -> bool:
    return (
        bool(existing_value)
        and bool(candidate_value)
        and not _has_conflicting_values(
            existing_value,
            candidate_value,
        )
    )
