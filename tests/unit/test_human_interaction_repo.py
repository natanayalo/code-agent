import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.base import Base
from db.enums import HumanInteractionStatus, HumanInteractionType, TaskStatus
from db.models import HumanInteraction, Task, User
from db.models import Session as ConversationSession
from repositories.sqlalchemy import HumanInteractionRepository


@pytest.fixture
def session():
    """Create an in-memory SQLite session for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    session = SessionFactory()
    yield session
    session.close()


@pytest.fixture
def repo(session: Session) -> HumanInteractionRepository:
    return HumanInteractionRepository(session)


@pytest.fixture
def task_id(session: Session) -> str:
    user = User(external_user_id="test-user")
    session.add(user)
    session.flush()
    conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="thread-1")
    session.add(conv)
    session.flush()
    task = Task(session_id=conv.id, task_text="test task", status=TaskStatus.PENDING)
    session.add(task)
    session.flush()
    return task.id


def test_record_response_success(repo: HumanInteractionRepository, task_id: str, session: Session):
    interaction = HumanInteraction(
        task_id=task_id,
        interaction_type=HumanInteractionType.CLARIFICATION,
        status=HumanInteractionStatus.PENDING,
        summary="Need clarification",
        data={"questions": ["What?"]},
    )
    session.add(interaction)
    session.flush()

    response_data = {"answer": "This."}
    updated, applied = repo.record_response(
        interaction_id=interaction.id,
        task_id=task_id,
        response_data=response_data,
        status=HumanInteractionStatus.RESOLVED,
    )

    assert updated is not None
    assert applied is True
    assert updated.status == HumanInteractionStatus.RESOLVED
    assert updated.response_data == response_data
    assert updated.updated_at is not None


def test_record_response_idempotency(
    repo: HumanInteractionRepository, task_id: str, session: Session
):
    interaction = HumanInteraction(
        task_id=task_id,
        interaction_type=HumanInteractionType.CLARIFICATION,
        status=HumanInteractionStatus.RESOLVED,
        summary="Need clarification",
        data={"questions": ["What?"]},
        response_data={"answer": "This."},
    )
    session.add(interaction)
    session.flush()

    # Identical response
    updated, applied = repo.record_response(
        interaction_id=interaction.id,
        task_id=task_id,
        response_data={"answer": "This."},
        status=HumanInteractionStatus.RESOLVED,
    )
    assert updated.id == interaction.id
    assert (
        applied is False
    )  # Idempotent terminal replay should not report a newly applied transition.

    # Mismatched response
    updated, applied = repo.record_response(
        interaction_id=interaction.id,
        task_id=task_id,
        response_data={"answer": "Something else."},
        status=HumanInteractionStatus.RESOLVED,
    )
    assert updated.id == interaction.id
    assert applied is False


def test_record_response_task_id_mismatch(
    repo: HumanInteractionRepository, task_id: str, session: Session
):
    interaction = HumanInteraction(
        task_id=task_id,
        interaction_type=HumanInteractionType.CLARIFICATION,
        status=HumanInteractionStatus.PENDING,
        summary="Need clarification",
        data={"questions": ["What?"]},
    )
    session.add(interaction)
    session.flush()

    updated, applied = repo.record_response(
        interaction_id=interaction.id,
        task_id="wrong-task-id",
        response_data={"answer": "This."},
        status=HumanInteractionStatus.RESOLVED,
    )
    assert updated is None
    assert applied is False


def test_record_response_not_found(repo: HumanInteractionRepository, task_id: str):
    updated, applied = repo.record_response(
        interaction_id="missing-id",
        task_id=task_id,
        response_data={"answer": "This."},
        status=HumanInteractionStatus.RESOLVED,
    )
    assert updated is None
    assert applied is False


def test_sync_task_spec_flags_does_not_reopen_resolved_same_resume_token(
    repo: HumanInteractionRepository, task_id: str, session: Session
):
    resolved = HumanInteraction(
        task_id=task_id,
        interaction_type=HumanInteractionType.CLARIFICATION,
        status=HumanInteractionStatus.RESOLVED,
        summary="Task requires clarification before execution can continue.",
        data={
            "source": "task_spec",
            "resume_token": f"clarification-{task_id}",
            "questions": ["Old question?"],
        },
        response_data={"answer": "Already answered"},
    )
    stale_pending = HumanInteraction(
        task_id=task_id,
        interaction_type=HumanInteractionType.CLARIFICATION,
        status=HumanInteractionStatus.PENDING,
        summary="Task requires clarification before execution can continue.",
        data={
            "source": "task_spec",
            "resume_token": f"clarification-{task_id}",
            "questions": ["Stale pending question?"],
        },
    )
    session.add_all([resolved, stale_pending])
    session.flush()

    repo.sync_task_spec_flags(
        task_id=task_id,
        task_spec={
            "requires_clarification": True,
            "clarification_questions": ["New wording should not reopen"],
        },
    )
    session.flush()

    rows = repo.list_by_task(task_id=task_id)
    clarification_rows = [
        row for row in rows if row.interaction_type == HumanInteractionType.CLARIFICATION
    ]
    assert len(clarification_rows) == 2
    assert any(row.status == HumanInteractionStatus.RESOLVED for row in clarification_rows)
    assert any(row.status == HumanInteractionStatus.CANCELLED for row in clarification_rows)
    assert not any(row.status == HumanInteractionStatus.PENDING for row in clarification_rows)


def test_sync_task_spec_flags_reopens_when_resume_token_differs(
    repo: HumanInteractionRepository, task_id: str, session: Session
):
    resolved = HumanInteraction(
        task_id=task_id,
        interaction_type=HumanInteractionType.CLARIFICATION,
        status=HumanInteractionStatus.RESOLVED,
        summary="Task requires clarification before execution can continue.",
        data={
            "source": "task_spec",
            "resume_token": "clarification-some-other-task",
            "questions": ["Old question?"],
        },
        response_data={"answer": "Already answered"},
    )
    session.add(resolved)
    session.flush()

    repo.sync_task_spec_flags(
        task_id=task_id,
        task_spec={
            "requires_clarification": True,
            "clarification_questions": ["Need fresh answer"],
        },
    )
    session.flush()

    rows = repo.list_by_task(task_id=task_id)
    clarification_rows = [
        row for row in rows if row.interaction_type == HumanInteractionType.CLARIFICATION
    ]
    assert len(clarification_rows) == 2
    assert any(row.status == HumanInteractionStatus.RESOLVED for row in clarification_rows)
    assert any(row.status == HumanInteractionStatus.PENDING for row in clarification_rows)
