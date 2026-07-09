"""Deterministic evaluation harness for skeptical-memory extraction and admission quality."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator
from sqlalchemy import delete, select

from db.base import utc_now
from db.models import (
    MemoryAdmissionDecision as DBAdmissionDecision,
)
from db.models import (
    MemoryObservation,
    MemoryProposal,
    PersonalMemory,
    ProjectMemory,
    Task,
    User,
)
from db.models import Session as ConversationSession
from memory.observation import ObservationMemoryBridge
from repositories import (
    ObservationRepository,
    session_scope,
)


@dataclass(frozen=True, slots=True)
class ExpectedCandidate:
    """Expected properties of an extracted memory candidate."""

    memory_key: str | None = None
    category: str | None = None
    value: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MemoryExtractionObservation:
    """Trace observation seed data for evaluation."""

    source: str
    event_type: str
    summary: str
    content: str
    metadata_payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MemoryExtractionCase:
    """One test case defining task, constraints, input observations, and expected outcomes."""

    case_id: str
    task_text: str
    repo_url: str | None = None
    task_spec: dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None
    observations: tuple[MemoryExtractionObservation, ...] = ()
    expected_candidates: tuple[ExpectedCandidate, ...] = ()
    expected_absent: tuple[ExpectedCandidate, ...] = ()
    expected_admission_decisions: tuple[str, ...] | None = None
    expected_proposals: tuple[str, ...] | None = None
    expected_durable_memory_keys: tuple[str, ...] | None = None
    expected_rejections: int | None = None


@dataclass(frozen=True, slots=True)
class MemoryExtractionSuite:
    """A deterministic evaluation suite for memory extraction."""

    suite_name: str
    cases: tuple[MemoryExtractionCase, ...]


@dataclass(frozen=True, slots=True)
class MemoryExtractionCaseResult:
    """Per-case evaluation result details."""

    case_id: str
    passed: bool
    tp_count: int
    fp_count: int
    fn_count: int
    precision: float | None
    recall: float | None
    failures: tuple[str, ...]
    extracted_candidates_count: int
    decision_counts: dict[str, int]
    durable_write_count: int
    proposal_count: int
    rejected_count: int


@dataclass(frozen=True, slots=True)
class MemoryExtractionReport:
    """Aggregate report for a memory extraction evaluation run."""

    suite_name: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    precision: float | None
    recall: float | None
    results: tuple[MemoryExtractionCaseResult, ...]
    quality_metrics: dict[str, Any]


# Pydantic schemas for loading JSON suite securely


class _ExpectedCandidatePayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    memory_key: str | None = None
    category: str | None = None
    value: dict[str, Any] | None = None


class _ExtractionObservationPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    source: str
    event_type: str
    summary: str
    content: str
    metadata_payload: dict[str, Any] | None = None


class _ExtractionCasePayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    case_id: str
    task_text: str
    repo_url: str | None = None
    task_spec: dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None
    observations: list[_ExtractionObservationPayload] = Field(default_factory=list)
    expected_candidates: list[_ExpectedCandidatePayload] = Field(default_factory=list)
    expected_absent: list[_ExpectedCandidatePayload] = Field(default_factory=list)
    expected_admission_decisions: list[str] | None = None
    expected_proposals: list[str] | None = None
    expected_durable_memory_keys: list[str] | None = None
    expected_rejections: int | None = None

    @field_validator("case_id", "task_text")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string.")
        return value


class _ExtractionSuitePayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    suite_name: str
    cases: list[_ExtractionCasePayload]


_SUITE_ADAPTER = TypeAdapter(_ExtractionSuitePayload)


def load_memory_extraction_suite(path: Path) -> MemoryExtractionSuite:
    """Load the memory extraction evaluation suite from JSON and validate schema."""
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    try:
        parsed = _SUITE_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise ValueError(f"Memory extraction suite validation failed: {str(exc)}")

    cases = []
    for c in parsed.cases:
        obs = tuple(
            MemoryExtractionObservation(
                source=o.source,
                event_type=o.event_type,
                summary=o.summary,
                content=o.content,
                metadata_payload=o.metadata_payload,
            )
            for o in c.observations
        )
        expected = tuple(
            ExpectedCandidate(
                memory_key=ec.memory_key,
                category=ec.category,
                value=ec.value,
            )
            for ec in c.expected_candidates
        )
        absent = tuple(
            ExpectedCandidate(
                memory_key=ec.memory_key,
                category=ec.category,
                value=ec.value,
            )
            for ec in c.expected_absent
        )
        cases.append(
            MemoryExtractionCase(
                case_id=c.case_id,
                task_text=c.task_text,
                repo_url=c.repo_url,
                task_spec=c.task_spec,
                constraints=c.constraints,
                observations=obs,
                expected_candidates=expected,
                expected_absent=absent,
                expected_admission_decisions=(
                    None
                    if c.expected_admission_decisions is None
                    else tuple(c.expected_admission_decisions)
                ),
                expected_proposals=(
                    None if c.expected_proposals is None else tuple(c.expected_proposals)
                ),
                expected_durable_memory_keys=(
                    None
                    if c.expected_durable_memory_keys is None
                    else tuple(c.expected_durable_memory_keys)
                ),
                expected_rejections=c.expected_rejections,
            )
        )
    return MemoryExtractionSuite(suite_name=parsed.suite_name, cases=tuple(cases))


def _match_candidate(actual: dict[str, Any], expected: ExpectedCandidate) -> bool:
    """Check if actual candidate dictionary matches the expected properties."""
    if expected.memory_key is not None and actual.get("memory_key") != expected.memory_key:
        return False
    if expected.category is not None and actual.get("category") != expected.category:
        return False
    if expected.value is not None:
        actual_val = actual.get("value") or {}
        if not isinstance(actual_val, dict):
            return False
        for k, v in expected.value.items():
            if k not in actual_val or actual_val[k] != v:
                return False
    return True


def _clear_evaluation_tables(session: Any) -> None:
    """Clear all tables in session to isolate evaluation cases."""
    session.execute(delete(MemoryObservation))
    session.execute(delete(MemoryProposal))
    session.execute(delete(DBAdmissionDecision))
    session.execute(delete(PersonalMemory))
    session.execute(delete(ProjectMemory))
    session.execute(delete(Task))
    session.execute(delete(ConversationSession))
    session.execute(delete(User))
    session.flush()


def _seed_evaluation_case(
    case: MemoryExtractionCase,
    session: Any,
) -> None:
    """Seed base models and trace observations for a case."""
    user = User(external_user_id=f"user-{case.case_id}")
    session.add(user)
    session.flush()

    conv_session = ConversationSession(
        user_id=user.id,
        channel="eval",
        external_thread_id=f"thread-{case.case_id}",
    )
    session.add(conv_session)
    session.flush()

    task = Task(
        id=f"task-{case.case_id}",
        session_id=conv_session.id,
        task_text=case.task_text,
        repo_url=case.repo_url,
        task_spec=case.task_spec,
        constraints=case.constraints,
    )
    session.add(task)
    session.flush()

    obs_repo = ObservationRepository(session)
    for obs in case.observations:
        obs_repo.create(
            task_id=task.id,
            session_id=conv_session.id,
            repo_url=case.repo_url,
            source=obs.source,
            event_type=obs.event_type,
            observed_at=utc_now(),
            summary=obs.summary,
            content=obs.content,
            metadata_payload=obs.metadata_payload,
            admission_status="not_required",
        )
    session.flush()


def _evaluate_candidate_matching(
    actual_candidates: list[dict[str, Any]],
    expected_candidates: tuple[ExpectedCandidate, ...],
    expected_absent: tuple[ExpectedCandidate, ...],
    failures: list[str],
    fp_by_key: dict[str, int],
    fn_by_key: dict[str, int],
) -> tuple[int, int, int]:
    """Match actual vs expected candidates. Returns (tp, fp, fn)."""
    matched_actual_indices = set()
    matched_expected_indices = set()

    for exp_idx, expected in enumerate(expected_candidates):
        found = False
        for act_idx, actual in enumerate(actual_candidates):
            if act_idx in matched_actual_indices:
                continue
            if _match_candidate(actual, expected):
                matched_actual_indices.add(act_idx)
                matched_expected_indices.add(exp_idx)
                found = True
                break
        if not found:
            failures.append(
                f"Missing expected candidate: key={expected.memory_key}, "
                f"category={expected.category}"
            )
            key = expected.memory_key or "unknown"
            fn_by_key[key] = fn_by_key.get(key, 0) + 1

    matched_absent_indices = set()
    for expected_abs in expected_absent:
        for act_idx, actual in enumerate(actual_candidates):
            if _match_candidate(actual, expected_abs):
                matched_absent_indices.add(act_idx)
                failures.append(f"Extracted forbidden candidate: key={expected_abs.memory_key}")
                key = expected_abs.memory_key or "unknown"
                fp_by_key[key] = fp_by_key.get(key, 0) + 1

    for act_idx, actual in enumerate(actual_candidates):
        if act_idx not in matched_actual_indices and act_idx not in matched_absent_indices:
            failures.append(f"Unexpected extra candidate extracted: {actual.get('memory_key')}")
            key = actual.get("memory_key") or "unknown"
            fp_by_key[key] = fp_by_key.get(key, 0) + 1

    tp = len(matched_expected_indices)
    fn = len(expected_candidates) - tp
    fp = len(actual_candidates) - tp
    return tp, fp, fn


def _evaluate_admission_outcomes(
    case: MemoryExtractionCase,
    actual_decisions: list[DBAdmissionDecision],
    actual_proposals: list[MemoryProposal],
    actual_personal: list[PersonalMemory],
    actual_project: list[ProjectMemory],
    failures: list[str],
) -> None:
    """Assert expected decisions, proposals, and durable memories match actuals."""
    if case.expected_admission_decisions is not None:
        actual_decision_types = [d.decision for d in actual_decisions]
        if sorted(actual_decision_types) != sorted(case.expected_admission_decisions):
            failures.append(
                f"Admission decisions mismatch. Expected {case.expected_admission_decisions}, "
                f"got {actual_decision_types}"
            )

    if case.expected_proposals is not None:
        actual_prop_keys = [p.memory_key for p in actual_proposals]
        if sorted(actual_prop_keys) != sorted(case.expected_proposals):
            failures.append(
                f"Proposals mismatch. Expected keys {case.expected_proposals}, "
                f"got {actual_prop_keys}"
            )

    if case.expected_durable_memory_keys is not None:
        actual_dur_keys = [m.memory_key for m in actual_personal + actual_project]
        if sorted(actual_dur_keys) != sorted(case.expected_durable_memory_keys):
            failures.append(
                f"Durable memory keys mismatch. Expected {case.expected_durable_memory_keys}, "
                f"got {actual_dur_keys}"
            )

    if case.expected_rejections is not None:
        rejection_count = sum(1 for d in actual_decisions if d.decision == "reject")
        if rejection_count != case.expected_rejections:
            failures.append(
                f"Rejections mismatch. Expected {case.expected_rejections}, "
                f"got {rejection_count}"
            )


def _query_case_outcomes(
    session_factory: Any,
    case_id: str,
) -> tuple[
    list[MemoryObservation],
    list[DBAdmissionDecision],
    list[MemoryProposal],
    list[PersonalMemory],
    list[ProjectMemory],
]:
    """Query actual case observations, decisions, proposals, and memories."""
    with session_scope(session_factory) as session:
        actual_obs = list(
            session.scalars(
                select(MemoryObservation).where(
                    MemoryObservation.task_id == f"task-{case_id}",
                    MemoryObservation.event_type == "extracted_candidate",
                )
            ).all()
        )
        actual_decisions = list(
            session.scalars(
                select(DBAdmissionDecision).where(DBAdmissionDecision.task_id == f"task-{case_id}")
            ).all()
        )
        actual_proposals = list(
            session.scalars(
                select(MemoryProposal).where(MemoryProposal.task_id == f"task-{case_id}")
            ).all()
        )
        actual_personal = list(session.scalars(select(PersonalMemory)).all())
        actual_project = list(session.scalars(select(ProjectMemory)).all())
    return (
        actual_obs,
        actual_decisions,
        actual_proposals,
        actual_personal,
        actual_project,
    )


def _evaluate_case(
    case: MemoryExtractionCase,
    session_factory: Any,
    fp_by_key: dict[str, int],
    fn_by_key: dict[str, int],
) -> tuple[MemoryExtractionCaseResult, int, int, int]:
    """Evaluate a single test case. Returns (result, tp, fp, fn)."""
    failures: list[str] = []
    with session_scope(session_factory) as session:
        _clear_evaluation_tables(session)
        _seed_evaluation_case(case, session)

    with session_scope(session_factory) as session:
        bridge_summary = ObservationMemoryBridge.bridge_observations(
            session, f"task-{case.case_id}"
        )
        session.flush()

    actual_obs, actual_decs, actual_props, actual_pers, actual_proj = _query_case_outcomes(
        session_factory, case.case_id
    )

    actual_candidates = []
    for obs in actual_obs:
        cand = obs.metadata_payload.get("memory_candidate")
        if isinstance(cand, dict):
            actual_candidates.append(cand)

    tp, fp, fn = _evaluate_candidate_matching(
        actual_candidates,
        case.expected_candidates,
        case.expected_absent,
        failures,
        fp_by_key,
        fn_by_key,
    )

    _evaluate_admission_outcomes(
        case,
        actual_decs,
        actual_props,
        actual_pers,
        actual_proj,
        failures,
    )

    decision_counts = bridge_summary.get("decision_counts") or {}
    durable_write_count = bridge_summary.get("durable_memory_count") or 0
    proposal_count = bridge_summary.get("proposal_count") or 0
    actual_rejected_count = sum(1 for d in actual_decs if d.decision == "reject")

    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    passed = len(failures) == 0

    res = MemoryExtractionCaseResult(
        case_id=case.case_id,
        passed=passed,
        tp_count=tp,
        fp_count=fp,
        fn_count=fn,
        precision=precision,
        recall=recall,
        failures=tuple(failures),
        extracted_candidates_count=len(actual_candidates),
        decision_counts=decision_counts,
        durable_write_count=durable_write_count,
        proposal_count=proposal_count,
        rejected_count=actual_rejected_count,
    )
    return res, durable_write_count, proposal_count, actual_rejected_count


def evaluate_memory_extraction(
    suite: MemoryExtractionSuite,
    session_factory: Any,
) -> MemoryExtractionReport:
    """Evaluate memory extraction and admission outcomes across the suite."""
    case_results = []
    total_tp, total_fp, total_fn = 0, 0, 0

    fp_by_key: dict[str, int] = {}
    fn_by_key: dict[str, int] = {}
    direct_write_total, proposal_total, rejected_total = 0, 0, 0

    for case in suite.cases:
        res, durable, prop, rej = _evaluate_case(case, session_factory, fp_by_key, fn_by_key)
        case_results.append(res)
        total_tp += res.tp_count
        total_fp += res.fp_count
        total_fn += res.fn_count
        direct_write_total += durable
        proposal_total += prop
        rejected_total += rej

    overall_precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else None
    overall_recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else None
    passed_cases = sum(1 for r in case_results if r.passed)
    failed_cases = len(case_results) - passed_cases

    quality_metrics = {
        "extraction_precision": overall_precision,
        "extraction_recall": overall_recall,
        "admission_non_rejected_rate": (
            (direct_write_total + proposal_total)
            / (direct_write_total + proposal_total + rejected_total)
            if (direct_write_total + proposal_total + rejected_total) > 0
            else None
        ),
        "false_positive_count_by_key": fp_by_key,
        "false_negative_count_by_key": fn_by_key,
        "direct_write_count": direct_write_total,
        "proposal_count": proposal_total,
        "rejected_count": rejected_total,
    }

    return MemoryExtractionReport(
        suite_name=suite.suite_name,
        total_cases=len(suite.cases),
        passed_cases=passed_cases,
        failed_cases=failed_cases,
        precision=overall_precision,
        recall=overall_recall,
        results=tuple(case_results),
        quality_metrics=quality_metrics,
    )


def write_memory_extraction_report(report: MemoryExtractionReport, output_path: Path) -> None:
    """Save the evaluation report as a JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_dict = {
        "suite_name": report.suite_name,
        "total_cases": report.total_cases,
        "passed_cases": report.passed_cases,
        "failed_cases": report.failed_cases,
        "precision": report.precision,
        "recall": report.recall,
        "quality_metrics": report.quality_metrics,
        "results": [
            {
                "case_id": r.case_id,
                "passed": r.passed,
                "tp_count": r.tp_count,
                "fp_count": r.fp_count,
                "fn_count": r.fn_count,
                "precision": r.precision,
                "recall": r.recall,
                "failures": list(r.failures),
                "extracted_candidates_count": r.extracted_candidates_count,
                "decision_counts": r.decision_counts,
                "durable_write_count": r.durable_write_count,
                "proposal_count": r.proposal_count,
                "rejected_count": r.rejected_count,
            }
            for r in report.results
        ],
    }
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report_dict, file, indent=2)
