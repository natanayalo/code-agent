"""Integration tests for human interaction endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from db.enums import HumanInteractionHitlMode, HumanInteractionStatus, HumanInteractionType
from repositories import session_scope


def test_list_pending_interactions_endpoint(
    client: TestClient,
    session_factory,
) -> None:
    """It should return pending interactions formatted as inbox cards."""
    # Create a task to anchor the interaction
    response = client.post(
        "/tasks",
        json={
            "task_text": "Task needing clarification",
            "session": {
                "channel": "http",
                "external_user_id": "http:test-inbox",
                "external_thread_id": "thread-inbox",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    # Directly insert a pending interaction simulating a blocked task
    with session_scope(session_factory) as session:
        from db.models import HumanInteraction

        interaction = HumanInteraction(
            task_id=task_id,
            interaction_type=HumanInteractionType.CLARIFICATION,
            status=HumanInteractionStatus.PENDING,
            summary="Need clarity on target repo",
            hitl_mode=HumanInteractionHitlMode.NOTIFY_ONLY,
            decision_key="decision_123",
            data={},
        )
        session.add(interaction)
        session.flush()
        interaction_id = interaction.id

    # Fetch the pending interactions endpoint
    inbox_response = client.get("/tasks/interactions/pending")
    assert inbox_response.status_code == 200
    cards = inbox_response.json()
    assert len(cards) >= 1

    # Find our card
    card = next(c for c in cards if c["task_id"] == task_id)

    assert card["task_id"] == task_id
    assert card["task_text"] == "Task needing clarification"
    assert card["status"] == "pending"

    # Assert inner interaction maps correctly
    interaction_data = card["interaction"]
    assert interaction_data["interaction_id"] == interaction_id
    assert interaction_data["summary"] == "Need clarity on target repo"
    assert interaction_data["interaction_type"] == "clarification"
    assert interaction_data["status"] == "pending"

    # Ensure our new fields decision_key and hitl_mode mapped successfully
    assert interaction_data["decision_key"] == "decision_123"
    assert interaction_data["hitl_mode"] == "notify_only"
