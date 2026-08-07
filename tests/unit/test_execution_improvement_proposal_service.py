"""Unit tests for orchestrator/execution_improvement_proposal_service.py."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.base import Base
from db.enums import ProposalType
from db.models import Session as ConversationSession
from db.models import User
from orchestrator import execution_improvement_proposal_service as eips
from orchestrator.improvement_suggestions import (
    ImprovementSuggestionScorer,
    ImprovementSuggestionScoringContext,
    ImprovementSuggestionScoringResult,
)
from orchestrator.reflection import FrictionReport, ImprovementSuggestion
from orchestrator.state import OrchestratorState, RouteDecision, TaskRequest


class DummyScorer(ImprovementSuggestionScorer):
    def __init__(
        self, result: ImprovementSuggestionScoringResult | None = None, raise_exc: bool = False
    ):
        self.result = result
        self.raise_exc = raise_exc

    async def score_improvement_suggestion(
        self,
        report: FrictionReport,
        deterministic_suggestion: ImprovementSuggestion,
        context: ImprovementSuggestionScoringContext,
    ) -> ImprovementSuggestionScoringResult | None:
        if self.raise_exc:
            raise RuntimeError("Scoring LLM crashed")
        return self.result


class DummyExecutionService:
    def __init__(self, session_factory: Any, scorer: Any = None, enable_llm: bool = False):
        self.session_factory = session_factory
        self.improvement_scorer = scorer
        self.enable_improvement_llm_scoring = enable_llm


@pytest.fixture
def db_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(db_engine):
    return sessionmaker(bind=db_engine)


def test_deterministic_scoring_result():
    sugg = ImprovementSuggestion(
        title="T1",
        description="D1",
        value="high",
        effort="small",
        risk="low",
        layer_impact="orchestrator",
        validation_path="val",
    )
    res = eips._deterministic_scoring_result(
        sugg, enabled=False, fallback=True, fallback_reason="test", provider="dummy"
    )
    assert res.suggestion == sugg
    assert res.metadata.enabled is False
    assert res.metadata.mode == "deterministic"
    assert res.metadata.fallback is True
    assert res.metadata.fallback_reason == "test"
    assert res.metadata.provider == "dummy"


@pytest.mark.anyio
async def test_score_improvement_suggestion_branches():
    sugg = ImprovementSuggestion(
        title="T1",
        description="D1",
        value="high",
        effort="small",
        risk="low",
        layer_impact="orchestrator",
        validation_path="val",
    )
    report = FrictionReport(task_id="t1", source="sandbox", description="desc", impact="blocked")
    context = ImprovementSuggestionScoringContext(
        task_id="t1",
        task_text="text",
        repo_url=None,
        branch=None,
        attempt_count=2,
        failure_kind=None,
        retry_context=True,
        session_id="s1",
        task_constraints={},
        task_budget={},
    )

    # 1. Disabled
    r1 = await eips._score_improvement_suggestion(
        None, enabled=False, report=report, deterministic_suggestion=sugg, context=context
    )
    assert r1.metadata.enabled is False
    assert r1.metadata.fallback_reason == "disabled"

    # 2. Enabled but scorer is None
    r2 = await eips._score_improvement_suggestion(
        None, enabled=True, report=report, deterministic_suggestion=sugg, context=context
    )
    assert r2.metadata.enabled is True
    assert r2.metadata.fallback is True
    assert r2.metadata.fallback_reason == "scorer_unavailable"

    # 3. Exception in scorer
    r3 = await eips._score_improvement_suggestion(
        DummyScorer(raise_exc=True),
        enabled=True,
        report=report,
        deterministic_suggestion=sugg,
        context=context,
    )
    assert r3.metadata.fallback is True
    assert "RuntimeError" in (r3.metadata.fallback_reason or "")

    # 4. Scorer returns None
    r4 = await eips._score_improvement_suggestion(
        DummyScorer(result=None),
        enabled=True,
        report=report,
        deterministic_suggestion=sugg,
        context=context,
    )
    assert r4.metadata.fallback is True
    assert r4.metadata.fallback_reason == "no_model_suggestion"

    # 5. Scorer success
    valid_res = eips._deterministic_scoring_result(
        sugg, enabled=True, fallback=False, fallback_reason=None
    )
    r5 = await eips._score_improvement_suggestion(
        DummyScorer(result=valid_res),
        enabled=True,
        report=report,
        deterministic_suggestion=sugg,
        context=context,
    )
    assert r5 == valid_res


def test_collect_friction_reports_parsing():
    task = TaskRequest(task_text="text")
    state = OrchestratorState(
        task=task,
        friction_reports=[
            FrictionReport(task_id="t1", source="sandbox", description="d1", impact="blocked")
        ],
    )

    # 1. No result friction reports
    reports1 = eips._collect_friction_reports(task_id="t1", state=state, worker_run_id="w1")
    assert len(reports1) == 1

    # 2. Result friction reports with valid/invalid entries
    class DummyResult:
        friction_reports = [
            "not a dict",
            {
                "source": "invalid_source",
                "impact": "invalid_impact",
                "description": "  valid desc  ",
            },
            {
                "source": "tooling",
                "impact": "slowed_down",
                "description": 12345,
                "context": {"extra": True},
            },
            {"invalid_keys": True},
        ]

    state.result = DummyResult()
    reports2 = eips._collect_friction_reports(task_id="t1", state=state, worker_run_id="w1")
    assert len(reports2) == 4
    # The invalid source/impact default to 'other' and 'unknown'
    assert reports2[1].source == "other"
    assert reports2[1].impact == "unknown"
    assert reports2[1].description == "valid desc"
    assert reports2[2].source == "tooling"
    assert reports2[2].impact == "slowed_down"
    assert reports2[2].description == "12345"


def test_has_retry_context():
    task = TaskRequest(task_text="text")
    state = OrchestratorState(task=task, attempt_count=1)

    assert eips._has_retry_context(state=state, task_constraints=None) is False

    # Attempt count > 1
    state.attempt_count = 2
    assert eips._has_retry_context(state=state, task_constraints=None) is True

    # Route reason contains retry
    state.attempt_count = 1
    state.route = RouteDecision(chosen_worker="codex", route_reason="Auto-retry after timeout")
    assert eips._has_retry_context(state=state, task_constraints=None) is True

    # Constraints contain verifier repair passes used
    state.route = None
    assert (
        eips._has_retry_context(
            state=state,
            task_constraints={eips.VERIFIER_REPAIR_PASSES_USED_CONSTRAINT: 1},
        )
        is True
    )


def test_build_friction_proposal_drafts():
    task = TaskRequest(task_text="text", repo_url="http://repo", branch="main")
    state = OrchestratorState(
        task=task,
        attempt_count=2,
        friction_reports=[
            FrictionReport(task_id="t1", source="sandbox", description="d1", impact="blocked"),
            FrictionReport(
                task_id="t1", source="sandbox", description="d1", impact="blocked"
            ),  # duplicate fingerprint
        ],
    )
    service = DummyExecutionService(None)

    drafts = eips._build_friction_proposal_drafts(
        service,
        task_id="t1",
        session_id="s1",
        task_constraints=None,
        state=state,
        worker_run_id="w1",
    )
    assert len(drafts) == 1
    assert drafts[0].fingerprint != ""


@pytest.mark.anyio
async def test_score_and_deterministically_score_friction_proposal_drafts():
    report = FrictionReport(task_id="t1", source="sandbox", description="d1", impact="blocked")
    sugg = ImprovementSuggestion(
        title="T1",
        description="D1",
        value="high",
        effort="small",
        risk="low",
        layer_impact="orchestrator",
        validation_path="val",
    )
    context = ImprovementSuggestionScoringContext(
        task_id="t1",
        task_text="t",
        repo_url=None,
        branch=None,
        attempt_count=2,
        failure_kind=None,
        retry_context=True,
        session_id="s1",
        task_constraints={},
        task_budget={},
    )
    draft = eips._FrictionProposalDraft(
        report=report,
        deterministic_suggestion=sugg,
        fingerprint="fp1",
        scoring_context=context,
        attempt_count=2,
        failure_kind=None,
        worker_type="codex",
    )

    scored_det = eips._deterministically_score_friction_proposal_drafts([draft])
    assert len(scored_det) == 1
    assert scored_det[0].scoring_result.metadata.mode == "deterministic"

    service = DummyExecutionService(None, scorer=DummyScorer(), enable_llm=False)
    scored = await eips._score_friction_proposal_drafts(service, drafts=[draft])
    assert len(scored) == 1


def _make_scored_proposal(session_id: str | None, fingerprint: str):
    report = FrictionReport(task_id="t1", source="sandbox", description="d1", impact="blocked")
    sugg = ImprovementSuggestion(
        title="T1",
        description="D1",
        value="high",
        effort="small",
        risk="low",
        layer_impact="orchestrator",
        validation_path="val",
    )
    context = ImprovementSuggestionScoringContext(
        task_id="t1",
        task_text="t",
        repo_url=None,
        branch=None,
        attempt_count=2,
        failure_kind=None,
        retry_context=True,
        session_id=session_id,
        task_constraints={},
        task_budget={},
    )
    draft = eips._FrictionProposalDraft(
        report=report,
        deterministic_suggestion=sugg,
        fingerprint=fingerprint,
        scoring_context=context,
        attempt_count=2,
        failure_kind=None,
        worker_type="codex",
    )
    res = eips._deterministic_scoring_result(
        sugg, enabled=False, fallback=False, fallback_reason=None
    )
    return eips._ScoredFrictionProposal(draft=draft, scoring_result=res)


def test_persist_scored_friction_proposals_basic(session_factory):
    service = DummyExecutionService(session_factory)
    eips._persist_scored_friction_proposals(service, scored_proposals=[])

    scored1 = _make_scored_proposal("s1", "fp1")
    scored_no_session = _make_scored_proposal(None, "fp2")

    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(
            id="s1", user_id=user.id, channel="test", external_thread_id="t1"
        )
        session.add(conv)
        session.commit()

    eips._persist_scored_friction_proposals(service, scored_proposals=[scored1, scored_no_session])

    with session_factory() as session:
        from repositories import ProposalRepository

        props = ProposalRepository(session).list_proposals(
            task_id="t1", proposal_type=ProposalType.REFLECTION
        )
        assert len(props) == 1
        assert props[0].title == "T1"


def test_persist_scored_friction_proposals_deduplication(session_factory):
    service = DummyExecutionService(session_factory)
    report = FrictionReport(task_id="t1", source="sandbox", description="d1", impact="blocked")
    sugg = ImprovementSuggestion(
        title="T1",
        description="D1",
        value="high",
        effort="small",
        risk="low",
        layer_impact="orchestrator",
        validation_path="val",
    )
    context = ImprovementSuggestionScoringContext(
        task_id="t1",
        task_text="t",
        repo_url=None,
        branch=None,
        attempt_count=2,
        failure_kind=None,
        retry_context=True,
        session_id="s1",
        task_constraints={},
        task_budget={},
    )
    draft1 = eips._FrictionProposalDraft(
        report=report,
        deterministic_suggestion=sugg,
        fingerprint="fp1_dedup",
        scoring_context=context,
        attempt_count=2,
        failure_kind=None,
        worker_type="codex",
    )
    res = eips._deterministic_scoring_result(
        sugg, enabled=False, fallback=False, fallback_reason=None
    )
    scored1 = eips._ScoredFrictionProposal(draft=draft1, scoring_result=res)

    with session_factory() as session:
        user = User(external_user_id="u2")
        session.add(user)
        session.flush()
        conv = ConversationSession(
            id="s1", user_id=user.id, channel="test", external_thread_id="t2"
        )
        session.add(conv)
        session.commit()

    eips._persist_scored_friction_proposals(service, scored_proposals=[scored1])
    # Persist duplicate draft1 -> should be skipped by fingerprint check
    eips._persist_scored_friction_proposals(service, scored_proposals=[scored1])
    with session_factory() as session:
        from repositories import ProposalRepository

        props = ProposalRepository(session).list_proposals(
            task_id="t1", proposal_type=ProposalType.REFLECTION
        )
        assert len(props) == 1


def test_persist_friction_proposals_if_needed(session_factory):
    service = DummyExecutionService(session_factory)
    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(
            id="s1", user_id=user.id, channel="test", external_thread_id="t1"
        )
        session.add(conv)
        session.commit()

    task = TaskRequest(task_text="text")
    state = OrchestratorState(
        task=task,
        attempt_count=2,
        friction_reports=[
            FrictionReport(task_id="t1", source="sandbox", description="d1", impact="blocked")
        ],
    )

    eips._persist_friction_proposals_if_needed(
        service,
        task_id="t1",
        session_id="s1",
        task_constraints=None,
        state=state,
        worker_run_id="w1",
    )

    with session_factory() as session:
        from repositories import ProposalRepository

        props = ProposalRepository(session).list_proposals(
            task_id="t1", proposal_type=ProposalType.REFLECTION
        )
        assert len(props) == 1
