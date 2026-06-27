"""Integration tests for human interaction repositories."""

from __future__ import annotations

from db.enums import HumanInteractionStatus, HumanInteractionType
from repositories import (
    HumanInteractionRepository,
    SessionRepository,
    TaskRepository,
    UserRepository,
    session_scope,
)


def test_human_interaction_repository_syncs_task_spec_flags(session_factory) -> None:
    """TaskSpec clarification/permission flags should map to resumable pending interactions."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        interaction_repo = HumanInteractionRepository(session)

        user = user_repo.create(
            external_user_id="telegram:interactions", display_name="Interactions"
        )
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-interactions",
        )
        task = task_repo.create(
            session_id=conversation_session.id, task_text="debug this and drop table"
        )

        task_spec = {
            "goal": "debug this and drop table",
            "requires_clarification": True,
            "requires_permission": True,
            "permission_reason": "Task is classified as high risk.",
            "risk_level": "high",
        }
        interaction_repo.sync_task_spec_flags(task_id=task.id, task_spec=task_spec)
        interaction_repo.sync_task_spec_flags(task_id=task.id, task_spec=task_spec)

        interactions = interaction_repo.list_by_task(task_id=task.id)
        assert len(interactions) == 2
        assert {interaction.interaction_type for interaction in interactions} == {
            HumanInteractionType.CLARIFICATION,
            HumanInteractionType.PERMISSION,
        }
        for interaction in interactions:
            assert interaction.status is HumanInteractionStatus.PENDING
            assert interaction.data["source"] == "task_spec"
            assert interaction.data["resume_token"].endswith(task.id)
        clarification = next(
            interaction
            for interaction in interactions
            if interaction.interaction_type is HumanInteractionType.CLARIFICATION
        )
        assert clarification.data["questions"] == [
            "What exact repo, files, behavior, or failure should the worker target for: "
            "debug this and drop table?"
        ]

        clarification.status = HumanInteractionStatus.RESOLVED
        session.flush()
        interaction_repo.sync_task_spec_flags(task_id=task.id, task_spec=task_spec)
        after_resync = interaction_repo.list_by_task(task_id=task.id)
        assert len(after_resync) == 2
        clarification_rows = [
            interaction
            for interaction in after_resync
            if interaction.interaction_type is HumanInteractionType.CLARIFICATION
        ]
        assert len(clarification_rows) == 1
        assert clarification_rows[0].status is HumanInteractionStatus.RESOLVED

        changed_task_spec = {
            **task_spec,
            "clarification_questions": ["Which migration file should be updated?"],
        }
        interaction_repo.sync_task_spec_flags(task_id=task.id, task_spec=changed_task_spec)
        after_changed_spec = interaction_repo.list_by_task(task_id=task.id)
        changed_clarification_rows = [
            interaction
            for interaction in after_changed_spec
            if interaction.interaction_type is HumanInteractionType.CLARIFICATION
        ]
        assert len(changed_clarification_rows) == 1
        assert changed_clarification_rows[0].status is HumanInteractionStatus.RESOLVED

        interaction_repo.sync_task_spec_flags(
            task_id=task.id,
            task_spec={"requires_clarification": False, "requires_permission": False},
        )
        refreshed = interaction_repo.list_by_task(task_id=task.id)
        assert len(refreshed) == 2
        assert any(
            interaction.status is HumanInteractionStatus.RESOLVED for interaction in refreshed
        )
        assert any(
            interaction.interaction_type is HumanInteractionType.PERMISSION
            and interaction.status is HumanInteractionStatus.CANCELLED
            for interaction in refreshed
        )


def test_human_interaction_repository_filters_statuses_and_reopens_materially_changed_checkpoints(
    session_factory,
) -> None:
    """Interaction listing filters and material changes should create fresh pending checkpoints."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        interaction_repo = HumanInteractionRepository(session)

        user = user_repo.create(
            external_user_id="telegram:interaction-filter",
            display_name="Filter",
        )
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-filter",
        )
        task = task_repo.create(session_id=conversation_session.id, task_text="needs clarification")

        interaction_repo.sync_task_spec_flags(
            task_id=task.id,
            task_spec={
                "goal": "needs clarification",
                "requires_clarification": True,
                "clarification_questions": ["Which file should change?"],
            },
        )
        initial = interaction_repo.list_by_task(
            task_id=task.id,
            interaction_types=(HumanInteractionType.CLARIFICATION,),
            statuses=(HumanInteractionStatus.PENDING,),
        )
        assert len(initial) == 1
        initial[0].status = HumanInteractionStatus.RESOLVED
        initial[0].data = {
            "source": "task_spec",
            "resume_token": "clarification-other-task",
            "questions": ["Which file should change?"],
        }
        initial[0].response_data = {"answer": "main.py"}
        session.flush()

        interaction_repo.sync_task_spec_flags(
            task_id=task.id,
            task_spec={
                "goal": "needs clarification",
                "requires_clarification": True,
                "clarification_questions": ["Which test should be updated too?"],
            },
        )

        pending = interaction_repo.list_by_task(
            task_id=task.id,
            interaction_types=(HumanInteractionType.CLARIFICATION,),
            statuses=(HumanInteractionStatus.PENDING,),
        )
        assert len(pending) == 1
        assert pending[0].data["questions"] == ["Which test should be updated too?"]


def test_human_interaction_repository_collapses_duplicate_pending_rows(session_factory) -> None:
    """Syncing TaskSpec flags should keep one live pending row and cancel stale duplicates."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        interaction_repo = HumanInteractionRepository(session)

        user = user_repo.create(
            external_user_id="telegram:interaction-dupes",
            display_name="Dupes",
        )
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-dupes",
        )
        task = task_repo.create(
            session_id=conversation_session.id,
            task_text="duplicate pending rows",
        )

        interaction_repo.sync_task_spec_flags(
            task_id=task.id,
            task_spec={"goal": "duplicate pending rows", "requires_clarification": True},
        )
        first = interaction_repo.list_by_task(task_id=task.id)[0]
        interaction_repo.session.add(
            first.__class__(
                task_id=task.id,
                interaction_type=first.interaction_type,
                status=HumanInteractionStatus.PENDING,
                summary="stale duplicate",
                data={
                    "source": "task_spec",
                    "resume_token": f"clarification-{task.id}",
                    "questions": ["stale"],
                },
            )
        )
        session.flush()

        interaction_repo.sync_task_spec_flags(
            task_id=task.id,
            task_spec={"goal": "duplicate pending rows", "requires_clarification": True},
        )

        rows = interaction_repo.list_by_task(task_id=task.id)
        clarification_rows = [
            row for row in rows if row.interaction_type is HumanInteractionType.CLARIFICATION
        ]
        assert len(clarification_rows) == 2
        assert sum(row.status is HumanInteractionStatus.PENDING for row in clarification_rows) == 1
        assert (
            sum(row.status is HumanInteractionStatus.CANCELLED for row in clarification_rows) == 1
        )


def test_human_interaction_list_pending_with_task_context(session_factory) -> None:
    """It should retrieve pending interactions joined with their associated task context."""
    from db.enums import HumanInteractionHitlMode

    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        interaction_repo = HumanInteractionRepository(session)

        user = user_repo.create(
            external_user_id="telegram:interaction-context",
            display_name="Context",
        )
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-context",
        )
        task = task_repo.create(session_id=conversation_session.id, task_text="contextual task")

        from db.models import HumanInteraction

        interaction = HumanInteraction(
            task_id=task.id,
            interaction_type=HumanInteractionType.CLARIFICATION,
            status=HumanInteractionStatus.PENDING,
            summary="Need context",
            hitl_mode=HumanInteractionHitlMode.NOTIFY_ONLY,
            decision_key="abcd123",
            data={},
        )
        session.add(interaction)
        session.flush()

        pending_with_context = interaction_repo.list_pending_with_task_context()
        assert len(pending_with_context) >= 1

        # Find our newly inserted row. Session is isolated, but let's be safe.
        row = next(r for r in pending_with_context if r[0].id == interaction.id)
        assert row[0].id == interaction.id
        assert row[0].hitl_mode == HumanInteractionHitlMode.NOTIFY_ONLY
        assert row[0].decision_key == "abcd123"
        assert row[1].task_text == "contextual task"
        assert row[1].priority == task.priority
