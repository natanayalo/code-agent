"""Unit tests for M23 memory admission."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import MemoryProposalStatus
from memory.admission import (
    CustomMemoryAdmissionService,
    MemoryCandidate,
)
from repositories import (
    MemoryAdmissionDecisionRepository,
    MemoryProposalRepository,
    ProjectMemoryRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)

FIXTURE_PATH = Path("tests/fixtures/memory_admission_spike_cases.json")


@pytest.fixture
def session_factory() -> sessionmaker:
    """Create an in-memory SQLite session factory for admission tests."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _fixture_cases() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text())


def _seed_preexisting_project_memory(session, case: dict) -> None:
    preexisting = case.get("preexisting")
    if not preexisting:
        return
    ProjectMemoryRepository(session).upsert(
        repo_url=preexisting["repo_url"],
        memory_key=preexisting["memory_key"],
        value=preexisting["value"],
    )


def test_custom_memory_admission_matches_fixture_contract(
    session_factory,
) -> None:
    """The deterministic admission service should honor the fixture contract."""
    with session_scope(session_factory) as session:
        for case in _fixture_cases():
            _seed_preexisting_project_memory(session, case)
            candidate = MemoryCandidate.model_validate(case["candidate"])

            result = CustomMemoryAdmissionService(session).admit_candidates(candidates=[candidate])

            assert result.results[0].decision == case["expected_decision"], case["name"]


def test_custom_admission_writes_low_risk_project_memory_directly(session_factory) -> None:
    """Verified low-risk project facts can bypass human review."""
    candidate = MemoryCandidate(
        category="project",
        repo_url="https://github.com/natanayalo/code-agent",
        memory_key="test_command",
        value={"command": ".venv/bin/pytest tests/unit"},
        confidence=0.95,
        evidence=["test: unit passed"],
        task_id="task-1",
        session_id="session-1",
    )

    with session_scope(session_factory) as session:
        result = CustomMemoryAdmissionService(session).admit_candidates(candidates=[candidate])
        stored = ProjectMemoryRepository(session).get(
            repo_url="https://github.com/natanayalo/code-agent",
            memory_key="test_command",
        )
        decisions = MemoryAdmissionDecisionRepository(session).list(task_id="task-1")

    assert result.decision_counts == {"create": 1}
    assert result.durable_write_count == 1
    assert stored is not None
    assert stored.value == {"command": ".venv/bin/pytest tests/unit"}
    assert decisions[0].decision == "create"
    assert decisions[0].durable_memory_id == stored.id


def test_custom_admission_routes_personal_memory_to_review(session_factory) -> None:
    """Personal preferences should become reviewable proposals."""
    candidate = MemoryCandidate(
        category="personal",
        memory_key="communication_preference",
        value={"style": "concise"},
        confidence=0.95,
        evidence=["explicit user instruction"],
    )

    with session_scope(session_factory) as session:
        result = CustomMemoryAdmissionService(session).admit_candidates(candidates=[candidate])
        proposals = MemoryProposalRepository(session).list(
            status=MemoryProposalStatus.PENDING_REVIEW
        )

    assert result.decision_counts == {"needs_human_review": 1}
    assert result.proposal_count == 1
    assert proposals[0].memory_key == "communication_preference"
    assert proposals[0].evidence["risk_level"] == "medium"


def test_custom_admission_rejects_secret_like_candidate(session_factory) -> None:
    """Secret-like candidates should be rejected instead of stored or proposed."""
    candidate = MemoryCandidate(
        category="project",
        repo_url="https://github.com/natanayalo/code-agent",
        memory_key="api_token",
        value={"redacted": True},
        confidence=0.99,
        evidence=["file: .env"],
    )

    with session_scope(session_factory) as session:
        result = CustomMemoryAdmissionService(session).admit_candidates(candidates=[candidate])
        proposals = MemoryProposalRepository(session).list()
        decisions = MemoryAdmissionDecisionRepository(session).list()

    assert result.rejected_count == 1
    assert proposals == []
    assert decisions[0].decision == "reject"
    assert decisions[0].risk_level == "blocked"


def test_custom_admission_allows_safe_auth_policy_language(session_factory) -> None:
    """Policy memories mentioning auth words should not be treated as secrets."""
    candidate = MemoryCandidate(
        category="project",
        repo_url="https://github.com/natanayalo/code-agent",
        memory_key="auth_policy",
        value={
            "guidance": (
                "Use token-based authentication in CLI docs and do not store "
                "passwords in plaintext."
            )
        },
        confidence=0.95,
        evidence=["docs/security.md"],
    )

    with session_scope(session_factory) as session:
        result = CustomMemoryAdmissionService(session).admit_candidates(candidates=[candidate])
        stored = ProjectMemoryRepository(session).get(
            repo_url="https://github.com/natanayalo/code-agent",
            memory_key="auth_policy",
        )

    assert result.decision_counts == {"create": 1}
    assert stored is not None


def test_custom_admission_handles_existing_memory_with_null_value(session_factory) -> None:
    """Admission should tolerate legacy or partially migrated rows with null values."""
    repo_url = "https://github.com/natanayalo/code-agent"
    with session_scope(session_factory) as session:
        stored = ProjectMemoryRepository(session).upsert(
            repo_url=repo_url,
            memory_key="verification",
            value={"unit": ".venv/bin/pytest tests/unit"},
        )
        stored.value = None
        candidate = MemoryCandidate(
            category="project",
            repo_url=repo_url,
            memory_key="verification",
            value={"integration": ".venv/bin/pytest tests/integration"},
            confidence=0.95,
            evidence=["test: integration passed"],
        )

        result = CustomMemoryAdmissionService(session).admit_candidates(candidates=[candidate])
        updated = ProjectMemoryRepository(session).get(
            repo_url=repo_url,
            memory_key="verification",
        )

    assert result.decision_counts == {"update": 1}
    assert updated is not None
    assert updated.value == {"integration": ".venv/bin/pytest tests/integration"}


def test_custom_admission_merges_non_conflicting_project_objects(session_factory) -> None:
    """Non-conflicting object updates can merge directly."""
    repo_url = "https://github.com/natanayalo/code-agent"
    with session_scope(session_factory) as session:
        ProjectMemoryRepository(session).upsert(
            repo_url=repo_url,
            memory_key="verification",
            value={"unit": ".venv/bin/pytest tests/unit"},
        )

        candidate = MemoryCandidate(
            category="project",
            repo_url=repo_url,
            memory_key="verification",
            value={"integration": ".venv/bin/pytest tests/integration"},
            confidence=0.95,
            evidence=["test: integration passed"],
        )
        result = CustomMemoryAdmissionService(session).admit_candidates(candidates=[candidate])
        stored = ProjectMemoryRepository(session).get(
            repo_url=repo_url,
            memory_key="verification",
        )

    assert result.decision_counts == {"merge": 1}
    assert stored is not None
    assert stored.value == {
        "unit": ".venv/bin/pytest tests/unit",
        "integration": ".venv/bin/pytest tests/integration",
    }


def test_custom_admission_rejects_embedded_snake_case_secret_keys(session_factory) -> None:
    """Embedded secret keys like openai_api_key or github_token should be rejected."""
    candidate = MemoryCandidate(
        category="project",
        repo_url="https://github.com/natanayalo/code-agent",
        memory_key="openai_api_key",
        value={"key": "some-value"},
        confidence=0.99,
        evidence=["config file"],
    )
    with session_scope(session_factory) as session:
        result = CustomMemoryAdmissionService(session).admit_candidates(candidates=[candidate])
    assert result.rejected_count == 1


def test_custom_admission_rejects_numeric_secrets(session_factory) -> None:
    """Numeric credentials like password=123456 should be rejected."""
    candidate = MemoryCandidate(
        category="project",
        repo_url="https://github.com/natanayalo/code-agent",
        memory_key="db_config",
        value={"password": 123456},
        confidence=0.99,
        evidence=["setup logs"],
    )
    with session_scope(session_factory) as session:
        result = CustomMemoryAdmissionService(session).admit_candidates(candidates=[candidate])
    assert result.rejected_count == 1


def test_custom_admission_defensive_against_legacy_non_dict_existing(session_factory) -> None:
    """If existing memory value is not a dict, merge/conflict handles it gracefully."""
    repo_url = "https://github.com/natanayalo/code-agent"
    with session_scope(session_factory) as session:
        # We need to manually set value to a list on a stored row
        stored = ProjectMemoryRepository(session).upsert(
            repo_url=repo_url,
            memory_key="some_key",
            value={"mock": "dict"},
        )
        stored.value = ["legacy", "list", "value"]
        session.flush()

        candidate = MemoryCandidate(
            category="project",
            repo_url=repo_url,
            memory_key="some_key",
            value={"new_dict_key": "val"},
            confidence=0.95,
            evidence=["new evidence"],
        )
        result = CustomMemoryAdmissionService(session).admit_candidates(candidates=[candidate])
        updated = ProjectMemoryRepository(session).get(repo_url=repo_url, memory_key="some_key")

    assert result.decision_counts == {"update": 1}
    assert updated is not None
    assert updated.value == {"new_dict_key": "val"}
