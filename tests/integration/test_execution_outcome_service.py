"""Integration tests for execution outcome service, including Proposal creation for Scout mode."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import ProposalStatus, ProposalType
from orchestrator.execution_improvement_proposal_service import (
    _build_friction_proposal_drafts,
    _persist_scored_friction_proposals,
    _score_friction_proposal_drafts,
)
from orchestrator.execution_outcome_service import _persist_execution_outcome
from orchestrator.improvement_suggestions import (
    ImprovementSuggestionScoringContext,
    ImprovementSuggestionScoringMetadata,
    ImprovementSuggestionScoringResult,
)
from orchestrator.reflection import FrictionReport, ImprovementSuggestion
from orchestrator.state import (
    ApprovalCheckpoint,
    OrchestratorState,
    RouteDecision,
    TaskRequest,
    TaskSpec,
    WorkerDispatch,
    WorkerResult,
)
from repositories import (
    ProposalRepository,
    SessionRepository,
    TaskRepository,
    UserRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)


class MockExecutionService:
    def __init__(
        self,
        session_factory,
        *,
        improvement_scorer: Any | None = None,
        enable_improvement_llm_scoring: bool = False,
    ):
        self.session_factory = session_factory
        self.retention_seconds = None
        self.improvement_scorer = improvement_scorer
        self.enable_improvement_llm_scoring = enable_improvement_llm_scoring

    def _prune_retained_runs(self, now: datetime) -> None:
        pass

    _build_friction_proposal_drafts = _build_friction_proposal_drafts
    _score_friction_proposal_drafts = _score_friction_proposal_drafts
    _persist_scored_friction_proposals = _persist_scored_friction_proposals


class RecordingScorer:
    """Test scorer returning a model-backed score override."""

    def __init__(self) -> None:
        self.calls: list[ImprovementSuggestionScoringContext] = []

    async def score_improvement_suggestion(
        self,
        *,
        report: FrictionReport,
        deterministic_suggestion: ImprovementSuggestion,
        context: ImprovementSuggestionScoringContext,
    ) -> ImprovementSuggestionScoringResult | None:
        del report
        self.calls.append(context)
        suggestion = deterministic_suggestion.model_copy(
            update={
                "effort": "medium",
                "risk": "medium",
                "layer_impact": "orchestrator",
                "hitl_need": "optional",
                "validation_path": "Run orchestrator persistence tests.",
            }
        )
        return ImprovementSuggestionScoringResult(
            suggestion=suggestion,
            metadata=ImprovementSuggestionScoringMetadata(
                enabled=True,
                mode="llm",
                provider="RecordingScorer",
                rationale="Repeated sandbox friction points at recovery policy.",
            ),
        )


class ExplodingScorer:
    """Test scorer that simulates a model timeout/failure."""

    async def score_improvement_suggestion(
        self,
        *,
        report: FrictionReport,
        deterministic_suggestion: ImprovementSuggestion,
        context: ImprovementSuggestionScoringContext,
    ) -> ImprovementSuggestionScoringResult | None:
        del report, deterministic_suggestion, context
        raise TimeoutError("scoring timed out")


@pytest.fixture
def session_factory():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _setup_task(session) -> str:
    user = UserRepository(session).create(
        external_user_id="test-scout",
        display_name="Test Scout",
    )
    sess = SessionRepository(session).create(
        user_id=user.id,
        channel="http",
        external_thread_id="thread-test",
    )
    task = TaskRepository(session).create(
        session_id=sess.id,
        task_text="Do scout stuff",
    )
    return task.id


async def _persist_outcome_and_score_improvements(
    service: MockExecutionService,
    *,
    task_id: str,
    state: OrchestratorState,
    now: datetime,
) -> None:
    persisted_outcome = _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
        persist_friction_proposals=False,
    )
    drafts = service._build_friction_proposal_drafts(
        task_id=persisted_outcome.task_id,
        session_id=persisted_outcome.session_id,
        task_constraints=persisted_outcome.task_constraints,
        state=state,
        worker_run_id=persisted_outcome.worker_run_id,
    )
    scored_proposals = await service._score_friction_proposal_drafts(drafts=drafts)
    service._persist_scored_friction_proposals(scored_proposals=scored_proposals)


def _make_sandbox_friction_state(
    task_id: str,
    *,
    task_text: str = "fix sandbox",
    constraints: dict[str, Any] | None = None,
    duplicate_reports: bool = False,
) -> OrchestratorState:
    task_kwargs: dict[str, Any] = {"task_text": task_text, "task_id": task_id}
    if constraints is not None:
        task_kwargs["constraints"] = constraints
    reports = [
        FrictionReport(
            task_id=task_id,
            source="sandbox",
            description="Infra crash prevented checkout.",
            impact="blocked",
            context={"failure_kind": "sandbox_infra"},
        )
    ]
    if duplicate_reports:
        reports.append(reports[0].model_copy())
    return OrchestratorState(
        task=TaskRequest(**task_kwargs),
        session=None,
        route=RouteDecision(chosen_worker="codex", route_reason="retry after infra failure"),
        dispatch=WorkerDispatch(),
        approval=ApprovalCheckpoint(required=False, status="not_required"),
        task_spec=TaskSpec(
            goal=task_text,
            task_type="maintenance",
            delivery_mode="workspace",
        ),
        result=WorkerResult(
            status="failure",
            summary="Sandbox infra crash blocked execution.",
            failure_kind="sandbox_infra",
            commands_run=[],
            test_results=[],
            artifacts=[],
        ),
        friction_reports=reports,
        attempt_count=2,
    )


def _assert_sandbox_improvement_proposal(proposal: Any, *, task_id: str) -> None:
    metadata = proposal.metadata_payload
    suggestion = metadata["improvement_suggestion"]
    friction_report = metadata["friction_report"]

    assert proposal.status == ProposalStatus.PENDING_REVIEW
    assert proposal.title == "Harden sandbox infrastructure recovery"
    assert proposal.summary == suggestion["description"]
    assert metadata["reflection_kind"] == "improvement_suggestion"
    assert friction_report["description"] == "Infra crash prevented checkout."
    assert friction_report["task_id"] == task_id
    assert friction_report["worker_run_id"]
    assert metadata["failure_kind"] == "sandbox_infra"
    assert metadata["fingerprint"]
    assert metadata["scoring"] == {
        "enabled": False,
        "mode": "deterministic",
        "provider": None,
        "rationale": None,
        "fallback": False,
        "fallback_reason": "disabled",
    }
    assert suggestion["value"] == "high"
    assert suggestion["effort"] == "large"
    assert suggestion["risk"] == "high"
    assert suggestion["layer_impact"] == "sandbox"
    assert suggestion["hitl_need"] == "required"


def test_persist_execution_outcome_creates_proposal_for_scout(session_factory) -> None:
    """Successful scout tasks should produce an idempotent Proposal in the Idea Inbox."""
    service = MockExecutionService(session_factory)

    with session_scope(session_factory) as session:
        task_id = _setup_task(session)
        task = TaskRepository(session).get(task_id)
        assert task is not None
        task_session_id = task.session_id

    state = OrchestratorState(
        task=TaskRequest(task_text="scout this", task_id=task_id),
        session=None,
        route=RouteDecision(chosen_worker="codex", route_reason="scout route"),
        dispatch=WorkerDispatch(),
        approval=ApprovalCheckpoint(required=False, status="not_required"),
        task_spec=TaskSpec(
            goal="scout this",
            task_type="scout",
            delivery_mode="summary",
        ),
        result=WorkerResult(
            status="success",
            summary="Found some interesting files.",
            files_changed=["a.txt"],
            commands_run=[],
            test_results=[],
            artifacts=[],
            budget_usage={"cost": 1.0},
            diff_text="diff content",
        ),
    )

    now = datetime.now(UTC)

    # First persistence call should create the proposal
    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(task_id=task_id)
        assert len(proposals) == 1
        assert proposals[0].session_id == task_session_id
        assert proposals[0].status == ProposalStatus.PENDING_REVIEW
        assert proposals[0].summary == "Found some interesting files."
        assert proposals[0].metadata_payload["source"] == "scout"
        assert proposals[0].metadata_payload["files_changed"] == ["a.txt"]
        assert proposals[0].metadata_payload["budget_usage"] == {"cost": 1.0}
        assert proposals[0].metadata_payload["diff_text"] == "diff content"

    # Second persistence call with same task_id should be idempotent
    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(task_id=task_id)
        assert len(proposals) == 1  # Still 1, idempotency works


def test_persist_execution_outcome_creates_scored_improvement_proposal(session_factory) -> None:
    """Repeated friction should produce an idempotent scored reflection proposal."""
    service = MockExecutionService(session_factory)

    with session_scope(session_factory) as session:
        task_id = _setup_task(session)

    state = OrchestratorState(
        task=TaskRequest(task_text="fix sandbox", task_id=task_id),
        session=None,
        route=RouteDecision(chosen_worker="codex", route_reason="retry after infra failure"),
        dispatch=WorkerDispatch(),
        approval=ApprovalCheckpoint(required=False, status="not_required"),
        task_spec=TaskSpec(
            goal="fix sandbox",
            task_type="maintenance",
            delivery_mode="workspace",
        ),
        result=WorkerResult(
            status="failure",
            summary="Sandbox infra crash blocked execution.",
            failure_kind="sandbox_infra",
            commands_run=[],
            test_results=[],
            artifacts=[],
        ),
        friction_reports=[
            FrictionReport(
                task_id=task_id,
                source="sandbox",
                description="Infra crash prevented checkout.",
                impact="blocked",
                context={"failure_kind": "sandbox_infra"},
            )
        ],
        attempt_count=2,
    )
    now = datetime.now(UTC)

    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )
        assert len(proposals) == 1
        _assert_sandbox_improvement_proposal(proposals[0], task_id=task_id)

    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )
        assert len(proposals) == 1


@pytest.mark.anyio
async def test_persist_execution_outcome_applies_llm_improvement_scoring(session_factory) -> None:
    """Enabled model scoring should revise scores and persist scorer rationale."""
    scorer = RecordingScorer()
    service = MockExecutionService(
        session_factory,
        improvement_scorer=scorer,
        enable_improvement_llm_scoring=True,
    )

    with session_scope(session_factory) as session:
        task_id = _setup_task(session)

    state = _make_sandbox_friction_state(
        task_id,
        constraints={"operator": "local"},
    )
    now = datetime.now(UTC)

    persisted_outcome = _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
        persist_friction_proposals=False,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )
        assert proposals == []
    assert scorer.calls == []

    drafts = service._build_friction_proposal_drafts(
        task_id=persisted_outcome.task_id,
        session_id=persisted_outcome.session_id,
        task_constraints=persisted_outcome.task_constraints,
        state=state,
        worker_run_id=persisted_outcome.worker_run_id,
    )
    scored_proposals = await service._score_friction_proposal_drafts(drafts=drafts)
    service._persist_scored_friction_proposals(scored_proposals=scored_proposals)

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )
        assert len(proposals) == 1
        metadata = proposals[0].metadata_payload
        suggestion = metadata["improvement_suggestion"]
        assert scorer.calls[0].session_id == proposals[0].session_id

    assert scorer.calls[0].task_id == task_id
    assert scorer.calls[0].task_constraints == {"operator": "local"}
    assert metadata["scoring"] == {
        "enabled": True,
        "mode": "llm",
        "provider": "RecordingScorer",
        "rationale": "Repeated sandbox friction points at recovery policy.",
        "fallback": False,
        "fallback_reason": None,
    }
    assert suggestion["effort"] == "medium"
    assert suggestion["risk"] == "medium"
    assert suggestion["layer_impact"] == "orchestrator"
    assert suggestion["hitl_need"] == "optional"


@pytest.mark.anyio
async def test_persist_execution_outcome_falls_back_when_llm_scoring_fails(session_factory) -> None:
    """Scorer failures should keep deterministic suggestions and record fallback metadata."""
    service = MockExecutionService(
        session_factory,
        improvement_scorer=ExplodingScorer(),
        enable_improvement_llm_scoring=True,
    )

    with session_scope(session_factory) as session:
        task_id = _setup_task(session)

    state = _make_sandbox_friction_state(task_id)
    now = datetime.now(UTC)

    await _persist_outcome_and_score_improvements(
        service,
        task_id=task_id,
        state=state,
        now=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )
        metadata = proposals[0].metadata_payload
        suggestion = metadata["improvement_suggestion"]

    assert metadata["scoring"]["enabled"] is True
    assert metadata["scoring"]["mode"] == "deterministic"
    assert metadata["scoring"]["provider"] == "ExplodingScorer"
    assert metadata["scoring"]["fallback"] is True
    assert "TimeoutError: scoring timed out" in metadata["scoring"]["fallback_reason"]
    assert suggestion["effort"] == "large"
    assert suggestion["risk"] == "high"
    assert suggestion["hitl_need"] == "required"


def test_persist_execution_outcome_skips_scorer_when_llm_scoring_disabled(
    session_factory,
) -> None:
    """Disabled scoring flag should not call the scorer even if one is configured."""
    scorer = RecordingScorer()
    service = MockExecutionService(
        session_factory,
        improvement_scorer=scorer,
        enable_improvement_llm_scoring=False,
    )

    with session_scope(session_factory) as session:
        task_id = _setup_task(session)

    state = OrchestratorState(
        task=TaskRequest(task_text="fix sandbox", task_id=task_id),
        session=None,
        route=RouteDecision(chosen_worker="codex", route_reason="retry after infra failure"),
        dispatch=WorkerDispatch(),
        approval=ApprovalCheckpoint(required=False, status="not_required"),
        task_spec=TaskSpec(
            goal="fix sandbox",
            task_type="maintenance",
            delivery_mode="workspace",
        ),
        result=WorkerResult(
            status="failure",
            summary="Sandbox infra crash blocked execution.",
            failure_kind="sandbox_infra",
            commands_run=[],
            test_results=[],
            artifacts=[],
        ),
        friction_reports=[
            FrictionReport(
                task_id=task_id,
                source="sandbox",
                description="Infra crash prevented checkout.",
                impact="blocked",
                context={"failure_kind": "sandbox_infra"},
            )
        ],
        attempt_count=2,
    )
    now = datetime.now(UTC)

    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )
        metadata = proposals[0].metadata_payload

    assert scorer.calls == []
    assert metadata["scoring"]["enabled"] is False
    assert metadata["scoring"]["mode"] == "deterministic"
    assert metadata["scoring"]["fallback"] is False
    assert metadata["scoring"]["fallback_reason"] == "disabled"


@pytest.mark.anyio
async def test_persist_execution_outcome_dedupes_same_pass_friction_reports(
    session_factory,
) -> None:
    """Duplicate friction reports in one outcome should create one proposal."""
    scorer = RecordingScorer()
    service = MockExecutionService(
        session_factory,
        improvement_scorer=scorer,
        enable_improvement_llm_scoring=True,
    )

    with session_scope(session_factory) as session:
        task_id = _setup_task(session)

    state = _make_sandbox_friction_state(
        task_id,
        task_text="fix duplicate friction",
        duplicate_reports=True,
    )
    now = datetime.now(UTC)

    await _persist_outcome_and_score_improvements(
        service,
        task_id=task_id,
        state=state,
        now=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )
        assert len(proposals) == 1
        metadata = proposals[0].metadata_payload
        suggestion = metadata["improvement_suggestion"]

    assert len(scorer.calls) == 1
    assert metadata["fingerprint"]
    assert metadata["scoring"]["mode"] == "llm"
    assert metadata["scoring"]["fallback"] is False
    assert suggestion["risk"] == "medium"


@pytest.mark.anyio
async def test_persist_scored_improvement_proposal_ignores_scout_fingerprints(
    session_factory,
) -> None:
    """Scout proposal fingerprints should not suppress reflection suggestions."""
    service = MockExecutionService(session_factory)

    with session_scope(session_factory) as session:
        task_id = _setup_task(session)
        task = TaskRepository(session).get(task_id)
        assert task is not None
        task_session_id = task.session_id

    state = _make_sandbox_friction_state(task_id)
    now = datetime.now(UTC)
    persisted_outcome = _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
        persist_friction_proposals=False,
    )
    drafts = service._build_friction_proposal_drafts(
        task_id=persisted_outcome.task_id,
        session_id=persisted_outcome.session_id,
        task_constraints=persisted_outcome.task_constraints,
        state=state,
        worker_run_id=persisted_outcome.worker_run_id,
    )
    assert len(drafts) == 1

    with session_scope(session_factory) as session:
        ProposalRepository(session).create_proposal(
            session_id=task_session_id,
            task_id=task_id,
            title="Scout finding",
            summary="Scout proposal with a colliding fingerprint.",
            proposal_type=ProposalType.SCOUT,
            metadata_payload={"fingerprint": drafts[0].fingerprint},
        )

    scored_proposals = await service._score_friction_proposal_drafts(drafts=drafts)
    service._persist_scored_friction_proposals(scored_proposals=scored_proposals)

    with session_scope(session_factory) as session:
        scout_proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.SCOUT,
        )
        reflection_proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )

    assert len(scout_proposals) == 1
    assert len(reflection_proposals) == 1
    assert reflection_proposals[0].metadata_payload["fingerprint"] == drafts[0].fingerprint


@pytest.mark.anyio
async def test_persist_scored_improvement_proposal_skips_missing_session_without_dedupe(
    session_factory,
) -> None:
    """A skipped missing-session draft should not suppress a later valid match."""
    service = MockExecutionService(session_factory)

    with session_scope(session_factory) as session:
        task_id = _setup_task(session)

    state = _make_sandbox_friction_state(task_id)
    now = datetime.now(UTC)
    persisted_outcome = _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
        persist_friction_proposals=False,
    )
    drafts = service._build_friction_proposal_drafts(
        task_id=persisted_outcome.task_id,
        session_id=persisted_outcome.session_id,
        task_constraints=persisted_outcome.task_constraints,
        state=state,
        worker_run_id=persisted_outcome.worker_run_id,
    )
    scored_proposals = await service._score_friction_proposal_drafts(drafts=drafts)
    valid_scored_proposal = scored_proposals[0]
    missing_session_scored_proposal = replace(
        valid_scored_proposal,
        draft=replace(
            valid_scored_proposal.draft,
            scoring_context=replace(
                valid_scored_proposal.draft.scoring_context,
                session_id=None,
            ),
        ),
    )

    service._persist_scored_friction_proposals(
        scored_proposals=[
            missing_session_scored_proposal,
            valid_scored_proposal,
        ]
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )

    assert len(proposals) == 1
    assert proposals[0].metadata_payload["fingerprint"] == valid_scored_proposal.draft.fingerprint


def test_persist_execution_outcome_scores_worker_friction_report_dict(session_factory) -> None:
    """Worker-emitted friction dicts should be parsed and persisted as suggestions."""
    service = MockExecutionService(session_factory)

    with session_scope(session_factory) as session:
        task_id = _setup_task(session)

    state = OrchestratorState(
        task=TaskRequest(task_text="complete after workaround", task_id=task_id),
        session=None,
        route=RouteDecision(chosen_worker="gemini", route_reason="retry same worker"),
        dispatch=WorkerDispatch(),
        approval=ApprovalCheckpoint(required=False, status="not_required"),
        task_spec=TaskSpec(
            goal="complete after workaround",
            task_type="maintenance",
            delivery_mode="workspace",
        ),
        result=WorkerResult(
            status="success",
            summary="Completed after workaround.",
            commands_run=[],
            test_results=[],
            artifacts=[],
            friction_reports=[
                {
                    "source": "instructions",
                    "description": "Instructions forced an unnecessary workaround.",
                    "impact": "required_workaround",
                    "context": {"origin": "worker"},
                }
            ],
        ),
        attempt_count=2,
    )
    now = datetime.now(UTC)

    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )
        assert len(proposals) == 1
        metadata = proposals[0].metadata_payload
        suggestion = metadata["improvement_suggestion"]

        assert metadata["reflection_kind"] == "improvement_suggestion"
        assert metadata["friction_report"]["context"] == {"origin": "worker"}
        assert suggestion["value"] == "high"
        assert suggestion["effort"] == "small"
        assert suggestion["risk"] == "low"
        assert suggestion["layer_impact"] == "worker"
        assert suggestion["hitl_need"] == "none"
