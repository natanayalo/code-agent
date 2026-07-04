"""Integration tests for memory admission decision persistence."""

from __future__ import annotations

from repositories import MemoryAdmissionDecisionRepository, session_scope


def test_memory_admission_decision_repository_creates_and_filters(session_factory) -> None:
    """Admission decisions should be inspectable by task or session."""
    with session_scope(session_factory) as session:
        repo = MemoryAdmissionDecisionRepository(session)
        first = repo.create(
            category="project",
            memory_key="test_command",
            candidate_payload={"memory_key": "test_command"},
            decision="create",
            risk_level="low",
            reason="low-risk evidenced project memory can be created.",
            task_id="task-1",
            session_id="session-1",
            durable_memory_id="memory-1",
        )
        second = repo.create(
            category="personal",
            memory_key="communication_preference",
            candidate_payload={"memory_key": "communication_preference"},
            decision="needs_human_review",
            risk_level="medium",
            reason="personal memory requires human review.",
            task_id="task-2",
            session_id="session-1",
            proposal_id="proposal-1",
        )

        by_task = repo.list(task_id="task-1")
        by_session = repo.list(session_id="session-1")

    assert by_task == [first]
    assert {row.id for row in by_session} == {first.id, second.id}
