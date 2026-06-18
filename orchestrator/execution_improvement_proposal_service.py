"""Improvement proposal drafting, scoring, and persistence helpers."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from db.enums import ProposalStatus, ProposalType
from orchestrator.improvement_suggestions import (
    ImprovementSuggestionScorer,
    ImprovementSuggestionScoringContext,
    ImprovementSuggestionScoringMetadata,
    ImprovementSuggestionScoringResult,
    build_improvement_suggestion_draft,
)
from orchestrator.nodes.verification_result import VERIFIER_REPAIR_PASSES_USED_CONSTRAINT
from orchestrator.reflection import FrictionReport, ImprovementSuggestion
from orchestrator.state import OrchestratorState
from repositories import ProposalRepository, session_scope

logger = logging.getLogger("orchestrator.execution")


@dataclass(frozen=True)
class _FrictionProposalDraft:
    """Deterministic proposal draft ready for optional async scoring."""

    report: FrictionReport
    deterministic_suggestion: ImprovementSuggestion
    fingerprint: str
    scoring_context: ImprovementSuggestionScoringContext
    attempt_count: int
    failure_kind: str | None
    worker_type: str | None


@dataclass(frozen=True)
class _ScoredFrictionProposal:
    """Proposal draft with final scoring metadata."""

    draft: _FrictionProposalDraft
    scoring_result: ImprovementSuggestionScoringResult


def _deterministic_scoring_result(
    suggestion: ImprovementSuggestion,
    *,
    enabled: bool,
    fallback: bool,
    fallback_reason: str | None,
    provider: str | None = None,
) -> ImprovementSuggestionScoringResult:
    return ImprovementSuggestionScoringResult(
        suggestion=suggestion,
        metadata=ImprovementSuggestionScoringMetadata(
            enabled=enabled,
            mode="deterministic",
            provider=provider,
            fallback=fallback,
            fallback_reason=fallback_reason,
        ),
    )


async def _score_improvement_suggestion(
    scorer: ImprovementSuggestionScorer | None,
    *,
    enabled: bool,
    report: FrictionReport,
    deterministic_suggestion: ImprovementSuggestion,
    context: ImprovementSuggestionScoringContext,
) -> ImprovementSuggestionScoringResult:
    if not enabled:
        return _deterministic_scoring_result(
            deterministic_suggestion,
            enabled=False,
            fallback=False,
            fallback_reason="disabled",
        )
    provider = type(scorer).__name__ if scorer is not None else None
    if scorer is None:
        return _deterministic_scoring_result(
            deterministic_suggestion,
            enabled=True,
            fallback=True,
            fallback_reason="scorer_unavailable",
        )
    try:
        result = await scorer.score_improvement_suggestion(
            report=report,
            deterministic_suggestion=deterministic_suggestion,
            context=context,
        )
    except Exception as exc:
        logger.warning(
            "Model-backed improvement scoring failed; using deterministic scoring",
            extra={
                "task_id": context.task_id,
                "provider": provider,
                "error_type": type(exc).__name__,
            },
        )
        return _deterministic_scoring_result(
            deterministic_suggestion,
            enabled=True,
            fallback=True,
            fallback_reason=f"{type(exc).__name__}: {exc}",
            provider=provider,
        )
    if result is None:
        return _deterministic_scoring_result(
            deterministic_suggestion,
            enabled=True,
            fallback=True,
            fallback_reason="no_model_suggestion",
            provider=provider,
        )
    return result


def _collect_friction_reports(
    *,
    task_id: str,
    state: OrchestratorState,
    worker_run_id: str,
) -> list[FrictionReport]:
    reports: list[FrictionReport] = list(state.friction_reports)
    result_reports = (
        getattr(state.result, "friction_reports", None) if state.result is not None else None
    )
    if not result_reports:
        return reports

    for rep_dict in result_reports:
        if not isinstance(rep_dict, Mapping):
            logger.warning("Friction report from worker is not a mapping: %r", rep_dict)
            continue
        if not isinstance(rep_dict, dict):
            rep_dict = dict(rep_dict)
        try:
            source = rep_dict.get("source")
            if source not in {"tooling", "orchestrator", "sandbox", "instructions", "other"}:
                source = "other"
            impact = rep_dict.get("impact")
            if impact not in {"slowed_down", "blocked", "required_workaround", "unknown"}:
                impact = "unknown"
            desc = rep_dict.get("description")
            if isinstance(desc, str):
                desc = desc.strip() or None
            elif desc is not None:
                desc = str(desc).strip() or None
            reports.append(
                FrictionReport(
                    task_id=task_id,
                    worker_run_id=worker_run_id,
                    source=source,  # type: ignore[arg-type]
                    description=desc,
                    impact=impact,  # type: ignore[arg-type]
                    context=rep_dict.get("context"),
                )
            )
        except (ValidationError, AttributeError) as exc:
            logger.warning("Failed to parse friction report dict from worker: %s", exc)
            logger.debug("Validation details: %s", exc, exc_info=True)
    return reports


def _has_retry_context(
    *,
    state: OrchestratorState,
    task_constraints: dict[str, Any] | None,
) -> bool:
    if state.attempt_count > 1:
        return True
    if state.route and state.route.route_reason and "retry" in state.route.route_reason.lower():
        return True
    return bool(
        isinstance(task_constraints, dict)
        and VERIFIER_REPAIR_PASSES_USED_CONSTRAINT in task_constraints
    )


def _build_friction_proposal_drafts(
    self: Any,
    *,
    task_id: str,
    session_id: str,
    task_constraints: dict[str, Any] | None,
    state: OrchestratorState,
    worker_run_id: str,
) -> list[_FrictionProposalDraft]:
    all_reports = _collect_friction_reports(
        task_id=task_id,
        state=state,
        worker_run_id=worker_run_id,
    )
    if not all_reports:
        return []

    has_retry_context = _has_retry_context(state=state, task_constraints=task_constraints)
    if not has_retry_context:
        return []

    failure_kind = getattr(state.result, "failure_kind", None) if state.result else None
    seen_fingerprints: set[str] = set()
    proposal_drafts: list[_FrictionProposalDraft] = []

    for report in all_reports:
        report = report.model_copy(
            update={
                "task_id": report.task_id or task_id,
                "worker_run_id": report.worker_run_id or worker_run_id,
            }
        )
        draft = build_improvement_suggestion_draft(
            report,
            task_id=task_id,
            attempt_count=state.attempt_count,
            failure_kind=failure_kind,
            retry_context=has_retry_context,
        )

        if draft.fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(draft.fingerprint)
        proposal_drafts.append(
            _FrictionProposalDraft(
                report=report,
                deterministic_suggestion=draft.suggestion,
                fingerprint=draft.fingerprint,
                scoring_context=ImprovementSuggestionScoringContext(
                    task_id=task_id,
                    task_text=state.task.task_text,
                    repo_url=state.task.repo_url,
                    branch=state.task.branch,
                    attempt_count=state.attempt_count,
                    failure_kind=failure_kind,
                    retry_context=has_retry_context,
                    session_id=session_id,
                    task_constraints=task_constraints or state.task.constraints,
                    task_budget=state.task.budget,
                ),
                attempt_count=state.attempt_count,
                failure_kind=failure_kind,
                worker_type=state.route.chosen_worker if state.route else None,
            )
        )
    return proposal_drafts


async def _score_friction_proposal_drafts(
    self: Any,
    *,
    drafts: Sequence[_FrictionProposalDraft],
) -> list[_ScoredFrictionProposal]:
    scorer = getattr(self, "improvement_scorer", None)
    enabled = bool(getattr(self, "enable_improvement_llm_scoring", False))
    scored: list[_ScoredFrictionProposal] = []
    for draft in drafts:
        scoring_result = await _score_improvement_suggestion(
            scorer,
            enabled=enabled,
            report=draft.report,
            deterministic_suggestion=draft.deterministic_suggestion,
            context=draft.scoring_context,
        )
        scored.append(_ScoredFrictionProposal(draft=draft, scoring_result=scoring_result))
    return scored


def _deterministically_score_friction_proposal_drafts(
    drafts: Sequence[_FrictionProposalDraft],
) -> list[_ScoredFrictionProposal]:
    return [
        _ScoredFrictionProposal(
            draft=draft,
            scoring_result=_deterministic_scoring_result(
                draft.deterministic_suggestion,
                enabled=False,
                fallback=False,
                fallback_reason="disabled",
            ),
        )
        for draft in drafts
    ]


def _existing_improvement_fingerprints(
    proposal_repo: ProposalRepository,
    *,
    task_id: str,
) -> set[str]:
    existing_fingerprints: set[str] = set()
    for existing_proposal in proposal_repo.list_proposals(
        task_id=task_id,
        proposal_type=ProposalType.REFLECTION,
    ):
        metadata = existing_proposal.metadata_payload
        if not isinstance(metadata, dict):
            continue
        fingerprint = metadata.get("fingerprint")
        if isinstance(fingerprint, str):
            existing_fingerprints.add(fingerprint)
    return existing_fingerprints


def _persist_scored_friction_proposals(
    self: Any,
    *,
    scored_proposals: Sequence[_ScoredFrictionProposal],
) -> None:
    if not scored_proposals:
        return
    with session_scope(self.session_factory) as session:
        proposal_repo = ProposalRepository(session)
        fingerprints_by_task: dict[str, set[str]] = {}
        for scored_proposal in scored_proposals:
            draft = scored_proposal.draft
            context = draft.scoring_context
            task_id = context.task_id
            if task_id not in fingerprints_by_task:
                fingerprints_by_task[task_id] = _existing_improvement_fingerprints(
                    proposal_repo,
                    task_id=task_id,
                )
            task_fingerprints = fingerprints_by_task[task_id]
            if draft.fingerprint in task_fingerprints:
                continue
            if context.session_id is None:
                logger.warning(
                    "Skipping improvement suggestion without a session id",
                    extra={"task_id": task_id, "worker_run_id": draft.report.worker_run_id},
                )
                continue
            task_fingerprints.add(draft.fingerprint)

            scoring_result = scored_proposal.scoring_result
            proposal = proposal_repo.create_proposal(
                session_id=context.session_id,
                task_id=task_id,
                title=scoring_result.suggestion.title,
                summary=scoring_result.suggestion.description,
                status=ProposalStatus.PENDING_REVIEW,
                proposal_type=ProposalType.REFLECTION,
                metadata_payload={
                    "reflection_kind": "improvement_suggestion",
                    "improvement_suggestion": scoring_result.suggestion.model_dump(mode="json"),
                    "friction_report": draft.report.model_dump(mode="json"),
                    "scoring": scoring_result.metadata.to_payload(),
                    "attempt_count": draft.attempt_count,
                    "failure_kind": draft.failure_kind,
                    "worker_type": draft.worker_type,
                    "fingerprint": draft.fingerprint,
                },
            )
            logger.info(
                "Persisted improvement suggestion proposal",
                extra={
                    "task_id": task_id,
                    "proposal_id": proposal.id,
                    "worker_run_id": draft.report.worker_run_id,
                    "title": scoring_result.suggestion.title,
                },
            )


def _persist_friction_proposals_if_needed(
    self: Any,
    *,
    task_id: str,
    session_id: str,
    task_constraints: dict[str, Any] | None,
    state: OrchestratorState,
    worker_run_id: str,
) -> None:
    drafts = _build_friction_proposal_drafts(
        self,
        task_id=task_id,
        session_id=session_id,
        task_constraints=task_constraints,
        state=state,
        worker_run_id=worker_run_id,
    )
    _persist_scored_friction_proposals(
        self,
        scored_proposals=_deterministically_score_friction_proposal_drafts(drafts),
    )
