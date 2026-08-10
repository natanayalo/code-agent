"""Paired evaluation for M28 skeptical-memory effectiveness baselines."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from orchestrator.graph import build_load_memory_node
from orchestrator.state import MemoryEntry, OrchestratorState, SessionRef
from repositories import (
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    SessionRepository,
    SessionStateRepository,
    UserRepository,
    session_scope,
)

_DEFAULT_SUITE_PATH = Path(__file__).with_name("m28_memory_effectiveness_suite.json")
_SCENARIOS = frozenset(
    {"useful_hit", "irrelevant_rejection", "stale_reverification", "conflict_handling"}
)

Scenario = Literal[
    "useful_hit", "irrelevant_rejection", "stale_reverification", "conflict_handling"
]
MemoryCategory = Literal["personal", "project"]
ContextDisposition = Literal["available_to_worker", "suppressed", "not_retrieved"]
VerificationState = Literal["fresh", "stale", "requires_verification", "unverified"]


class EvaluationModel(BaseModel):
    """Strict base model for checked-in evaluation contracts and reports."""

    model_config = ConfigDict(extra="forbid", strict=True)


class MemoryFixture(EvaluationModel):
    """A durable, pre-admitted skeptical-memory fixture for an assisted phase."""

    category: MemoryCategory
    memory_key: str
    value: dict[str, Any]
    source: Literal["pre_admitted_fixture"] = "pre_admitted_fixture"
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    scope: str
    requires_verification: bool = False
    verification_age_days: int | None = Field(default=0, ge=0)

    @field_validator("memory_key", "scope")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string.")
        return value


class SessionFixture(EvaluationModel):
    """Compact state expected to survive the memory-assisted phase."""

    active_goal: str
    decisions_made: dict[str, Any]
    identified_risks: dict[str, Any]
    files_touched: list[str]

    @field_validator("active_goal")
    @classmethod
    def _require_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string.")
        return value


class CandidateExpectation(EvaluationModel):
    """Expected lifecycle evidence for one memory fixture."""

    category: MemoryCategory
    memory_key: str
    context_disposition: ContextDisposition
    verification_state: VerificationState
    required_reason_codes: list[str] = Field(default_factory=list)


class MemoryEffectivenessCase(EvaluationModel):
    """One cold/assisted repeated-task pair."""

    case_id: str
    scenario: Scenario
    task_text: str
    memory_fixtures: list[MemoryFixture]
    session_fixture: SessionFixture
    expected_candidates: list[CandidateExpectation]

    @field_validator("case_id", "task_text")
    @classmethod
    def _require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string.")
        return value

    @model_validator(mode="after")
    def _validate_candidates(self) -> MemoryEffectivenessCase:
        fixture_keys = {(item.category, item.memory_key) for item in self.memory_fixtures}
        expected_keys = {(item.category, item.memory_key) for item in self.expected_candidates}
        if fixture_keys != expected_keys:
            raise ValueError("expected_candidates must cover each memory fixture exactly once.")
        return self


class MemoryEffectivenessSuite(EvaluationModel):
    """The frozen, four-scenario M28 paired-evaluation contract."""

    suite_name: str
    schema_version: Literal[1]
    repo_url: str
    cases: list[MemoryEffectivenessCase]

    @model_validator(mode="after")
    def _validate_matrix(self) -> MemoryEffectivenessSuite:
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case IDs must be unique.")
        scenarios = {case.scenario for case in self.cases}
        if len(self.cases) != len(_SCENARIOS) or scenarios != _SCENARIOS:
            raise ValueError("suite must include each M28 scenario exactly once.")
        fixture_keys = [
            (fixture.category, fixture.memory_key)
            for case in self.cases
            for fixture in case.memory_fixtures
        ]
        if len(set(fixture_keys)) != len(fixture_keys):
            raise ValueError("memory fixture category/key pairs must be unique across the suite.")
        return self


class CandidateLifecycle(EvaluationModel):
    """Observed read-side lifecycle evidence for one pre-admitted fixture."""

    category: MemoryCategory
    memory_key: str
    source: str | None
    confidence: float
    scope: str | None
    requires_verification: bool
    last_verified_at: datetime | None
    verification_state: VerificationState
    retrieved: bool
    gate_status: str | None = None
    context_disposition: ContextDisposition
    reason_codes: list[str] = Field(default_factory=list)


class PhaseObservation(EvaluationModel):
    """Worker-visible loading result for either a cold or assisted phase."""

    retrieval_mode: str | None = None
    timeline_retrieval_mode: str | None = None
    search_query: str | None = None
    personal_keys: list[str] = Field(default_factory=list)
    project_keys: list[str] = Field(default_factory=list)
    session: dict[str, Any] = Field(default_factory=dict)
    candidates: list[CandidateLifecycle] = Field(default_factory=list)


class SessionContinuityResult(EvaluationModel):
    """Field-by-field compact-session continuity assertion."""

    passed: bool
    failures: list[str] = Field(default_factory=list)
    expected: SessionFixture
    actual: dict[str, Any]


class MemoryEffectivenessCaseResult(EvaluationModel):
    """Evidence and assertions for one cold/assisted pair."""

    case_id: str
    scenario: Scenario
    passed: bool
    failures: list[str] = Field(default_factory=list)
    cold: PhaseObservation
    assisted: PhaseObservation
    session_continuity: SessionContinuityResult


class MemoryEffectivenessReport(EvaluationModel):
    """Sorted M28.1 baseline report, suitable for local artifact storage."""

    schema_version: Literal[2] = 2
    suite_name: str
    retrieval_mode: str
    status: Literal["passed", "failed"]
    total_cases: int
    passed_cases: int
    failed_case_ids: list[str] = Field(default_factory=list)
    results: list[MemoryEffectivenessCaseResult]


def load_memory_effectiveness_suite(path: Path | None = None) -> MemoryEffectivenessSuite:
    """Load and validate the checked-in M28 paired-evaluation suite."""
    suite_path = path or _DEFAULT_SUITE_PATH
    try:
        return MemoryEffectivenessSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError(f"Memory effectiveness suite validation failed: {exc}") from exc


def _last_verified_at(fixture: MemoryFixture, *, evaluated_at: datetime) -> datetime | None:
    if fixture.verification_age_days is None:
        return None
    return evaluated_at - timedelta(days=fixture.verification_age_days)


def _clear_assisted_memory_fixtures(
    *, session_factory: Any, suite: MemoryEffectivenessSuite
) -> None:
    """Remove retained fixture rows so a repeated run starts cold."""
    with session_scope(session_factory) as session:
        personal_repo = PersonalMemoryRepository(session)
        project_repo = ProjectMemoryRepository(session)
        for case in suite.cases:
            for fixture in case.memory_fixtures:
                if fixture.category == "personal":
                    personal_repo.delete(memory_key=fixture.memory_key)
                else:
                    project_repo.delete(repo_url=suite.repo_url, memory_key=fixture.memory_key)


def _seed_assisted_contexts(
    *,
    session_factory: Any,
    suite: MemoryEffectivenessSuite,
    evaluated_at: datetime,
) -> dict[str, SessionRef]:
    session_refs: dict[str, SessionRef] = {}
    with session_scope(session_factory) as session:
        personal_repo = PersonalMemoryRepository(session)
        project_repo = ProjectMemoryRepository(session)
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        state_repo = SessionStateRepository(session)
        for case in sorted(suite.cases, key=lambda item: item.case_id):
            external_user_id = f"m28-eval:{case.case_id}"
            user = user_repo.get_by_external_user_id(external_user_id)
            if user is None:
                user = user_repo.create(external_user_id=external_user_id)
            external_thread_id = f"m28-{case.case_id}"
            conversation = session_repo.get_by_channel_thread(
                channel="evaluation", external_thread_id=external_thread_id
            )
            if conversation is None:
                conversation = session_repo.create(
                    user_id=user.id,
                    channel="evaluation",
                    external_thread_id=external_thread_id,
                )
            else:
                conversation.user_id = user.id
            compact_state = state_repo.upsert(
                session_id=conversation.id, **case.session_fixture.model_dump()
            )
            compact_state.active_goal = case.session_fixture.active_goal
            compact_state.decisions_made = case.session_fixture.decisions_made
            compact_state.identified_risks = case.session_fixture.identified_risks
            compact_state.files_touched = case.session_fixture.files_touched
            session_refs[case.case_id] = SessionRef(
                session_id=conversation.id,
                user_id=user.id,
                channel=conversation.channel,
                external_thread_id=conversation.external_thread_id,
            )
            for fixture in case.memory_fixtures:
                kwargs = {
                    "memory_key": fixture.memory_key,
                    "value": fixture.value,
                    "source": fixture.source,
                    "confidence": fixture.confidence,
                    "scope": fixture.scope,
                    "requires_verification": fixture.requires_verification,
                    "last_verified_at": _last_verified_at(fixture, evaluated_at=evaluated_at),
                }
                if fixture.category == "personal":
                    personal_repo.upsert(**kwargs)
                else:
                    project_repo.upsert(repo_url=suite.repo_url, **kwargs)
    return session_refs


def _run_load_memory(
    *,
    load_memory_node: Any,
    case: MemoryEffectivenessCase,
    repo_url: str,
    session_ref: SessionRef | None,
) -> dict[str, Any]:
    state = OrchestratorState(
        task={"task_text": case.task_text, "repo_url": repo_url, "branch": "master"},
        session=session_ref,
    )
    return load_memory_node(state)


def _verification_state(
    *,
    requires_verification: bool,
    last_verified_at: datetime | None,
    evaluated_at: datetime,
) -> VerificationState:
    if requires_verification:
        return "requires_verification"
    if last_verified_at is None:
        return "unverified"
    if last_verified_at.tzinfo is None:
        verified_at = last_verified_at.replace(tzinfo=UTC)
    else:
        verified_at = last_verified_at.astimezone(UTC)
    if verified_at >= evaluated_at - timedelta(days=1):
        return "fresh"
    return "stale"


def _observed_fixture_entries(
    *, session_factory: Any, suite: MemoryEffectivenessSuite
) -> dict[str, dict[tuple[str, str], MemoryEntry]]:
    """Reload persisted fixture metadata, including entries the gate suppresses."""
    entries_by_case: dict[str, dict[tuple[str, str], MemoryEntry]] = {}
    with session_scope(session_factory) as session:
        personal_repo = PersonalMemoryRepository(session)
        project_repo = ProjectMemoryRepository(session)
        for case in suite.cases:
            entries: dict[tuple[str, str], MemoryEntry] = {}
            for fixture in case.memory_fixtures:
                if fixture.category == "personal":
                    row = personal_repo.get(memory_key=fixture.memory_key)
                else:
                    row = project_repo.get(repo_url=suite.repo_url, memory_key=fixture.memory_key)
                if row is not None:
                    entries[(fixture.category, fixture.memory_key)] = MemoryEntry(
                        memory_key=row.memory_key,
                        value=dict(row.value or {}),
                        source=row.source,
                        confidence=row.confidence,
                        scope=row.scope,
                        last_verified_at=row.last_verified_at,
                        requires_verification=row.requires_verification,
                    )
            entries_by_case[case.case_id] = entries
    return entries_by_case


def _diagnostic_entries(
    memory: dict[str, Any],
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    diagnostics = memory.get("gate_diagnostics") or {}
    accepted: dict[tuple[str, str], dict[str, Any]] = {}
    suppressed: dict[tuple[str, str], dict[str, Any]] = {}
    for category in ("personal", "project"):
        for entry in diagnostics.get(f"accepted_{category}", []):
            accepted[(category, entry["memory_key"])] = entry
        for entry in diagnostics.get(f"suppressed_{category}", []):
            suppressed[(category, entry["memory_key"])] = entry
    return accepted, suppressed


def _candidate_lifecycles(
    *,
    memory: dict[str, Any],
    fixtures: list[MemoryFixture],
    observed_entries: dict[tuple[str, str], MemoryEntry],
    evaluated_at: datetime,
) -> list[CandidateLifecycle]:
    accepted, suppressed = _diagnostic_entries(memory)
    worker_visible = {
        (category, entry["memory_key"]): MemoryEntry.model_validate(entry)
        for category in ("personal", "project")
        for entry in memory[category]
    }
    lifecycles: list[CandidateLifecycle] = []
    for fixture in sorted(fixtures, key=lambda item: (item.category, item.memory_key)):
        key = (fixture.category, fixture.memory_key)
        accepted_entry = accepted.get(key)
        suppressed_entry = suppressed.get(key)
        if accepted_entry is not None:
            disposition: ContextDisposition = "available_to_worker"
            reason_codes = list(accepted_entry.get("gate_reason_codes", []))
            gate_status = str(accepted_entry.get("gate_status"))
        elif suppressed_entry is not None:
            disposition = "suppressed"
            reason_codes = list(suppressed_entry.get("reason_codes", []))
            gate_status = "suppressed"
        else:
            disposition = "not_retrieved"
            reason_codes = ["not_retrieved_for_query"]
            gate_status = None
        observed = worker_visible.get(key)
        if observed is None and suppressed_entry is not None:
            observed = MemoryEntry.model_validate(suppressed_entry["memory"])
        if observed is None:
            observed = observed_entries.get(key)
        lifecycles.append(
            CandidateLifecycle(
                category=fixture.category,
                memory_key=fixture.memory_key,
                source=observed.source if observed is not None else None,
                confidence=observed.confidence if observed is not None else 0.0,
                scope=observed.scope if observed is not None else None,
                requires_verification=(
                    observed.requires_verification if observed is not None else False
                ),
                last_verified_at=observed.last_verified_at if observed is not None else None,
                verification_state=_verification_state(
                    requires_verification=(
                        observed.requires_verification if observed is not None else False
                    ),
                    last_verified_at=observed.last_verified_at if observed is not None else None,
                    evaluated_at=evaluated_at,
                ),
                retrieved=disposition != "not_retrieved",
                gate_status=gate_status,
                context_disposition=disposition,
                reason_codes=sorted(reason_codes),
            )
        )
    return lifecycles


def _phase_observation(
    *,
    result: dict[str, Any],
    retrieval_mode: str,
    fixtures: list[MemoryFixture] | None = None,
    observed_entries: dict[tuple[str, str], MemoryEntry] | None = None,
    evaluated_at: datetime | None = None,
) -> PhaseObservation:
    memory = result["memory"]
    payload = next(
        (
            event.payload
            for event in result.get("timeline_events", [])
            if isinstance(getattr(event, "payload", None), dict)
            and event.payload.get("retrieval_mode") is not None
        ),
        {},
    )
    return PhaseObservation(
        retrieval_mode=retrieval_mode,
        timeline_retrieval_mode=payload.get("retrieval_mode"),
        search_query=payload.get("search_query"),
        personal_keys=sorted(entry["memory_key"] for entry in memory["personal"]),
        project_keys=sorted(entry["memory_key"] for entry in memory["project"]),
        session=dict(memory["session"]),
        candidates=_candidate_lifecycles(
            memory=memory,
            fixtures=fixtures or [],
            observed_entries=observed_entries or {},
            evaluated_at=evaluated_at or datetime.now(UTC),
        ),
    )


def _session_continuity(
    *, expected: SessionFixture, actual: dict[str, Any]
) -> SessionContinuityResult:
    failures = [
        f"session.{field}"
        for field, expected_value in expected.model_dump().items()
        if actual.get(field) != expected_value
    ]
    return SessionContinuityResult(
        passed=not failures,
        failures=failures,
        expected=expected,
        actual=actual,
    )


def _assert_case(
    *,
    case: MemoryEffectivenessCase,
    cold: PhaseObservation,
    assisted: PhaseObservation,
) -> tuple[list[str], SessionContinuityResult]:
    failures: list[str] = []
    if cold.personal_keys or cold.project_keys or cold.session:
        failures.append("cold_context_leak")
    candidates = {(item.category, item.memory_key): item for item in assisted.candidates}
    fixtures = {(item.category, item.memory_key): item for item in case.memory_fixtures}
    for expected in case.expected_candidates:
        candidate = candidates[(expected.category, expected.memory_key)]
        fixture = fixtures[(expected.category, expected.memory_key)]
        prefix = f"candidate:{expected.category}:{expected.memory_key}"
        if candidate.context_disposition != expected.context_disposition:
            failures.append(f"{prefix}:context_disposition")
        if candidate.verification_state != expected.verification_state:
            failures.append(f"{prefix}:verification_state")
        for field in ("source", "confidence", "scope", "requires_verification"):
            if getattr(candidate, field) != getattr(fixture, field):
                failures.append(f"{prefix}:{field}")
        missing_reasons = set(expected.required_reason_codes) - set(candidate.reason_codes)
        failures.extend(f"{prefix}:reason:{reason}" for reason in sorted(missing_reasons))
    continuity = _session_continuity(expected=case.session_fixture, actual=assisted.session)
    failures.extend(continuity.failures)
    return failures, continuity


def _effective_retrieval_mode(session_factory: Any) -> str:
    """Report the repository search implementation used by this evaluation run."""
    session = session_factory()
    try:
        bind = session.get_bind()
        dialect = bind.dialect.name if bind is not None else "unknown"
    finally:
        session.close()
    if dialect == "postgresql":
        return "postgres_full_text"
    return f"{dialect}_substring_fallback"


def evaluate_memory_effectiveness(
    *, suite: MemoryEffectivenessSuite, session_factory: Any, search_limit: int = 20
) -> MemoryEffectivenessReport:
    """Run cold phases, seed durable fixtures, then run assisted phases."""
    load_memory_node = build_load_memory_node(session_factory, search_limit=search_limit)
    retrieval_mode = _effective_retrieval_mode(session_factory)
    _clear_assisted_memory_fixtures(session_factory=session_factory, suite=suite)
    cold_results = {
        case.case_id: _run_load_memory(
            load_memory_node=load_memory_node,
            case=case,
            repo_url=suite.repo_url,
            session_ref=None,
        )
        for case in sorted(suite.cases, key=lambda item: item.case_id)
    }
    evaluated_at = datetime.now(UTC)
    session_refs = _seed_assisted_contexts(
        session_factory=session_factory,
        suite=suite,
        evaluated_at=evaluated_at,
    )
    observed_entries = _observed_fixture_entries(session_factory=session_factory, suite=suite)
    results: list[MemoryEffectivenessCaseResult] = []
    for case in sorted(suite.cases, key=lambda item: item.case_id):
        cold = _phase_observation(result=cold_results[case.case_id], retrieval_mode=retrieval_mode)
        assisted = _phase_observation(
            result=_run_load_memory(
                load_memory_node=load_memory_node,
                case=case,
                repo_url=suite.repo_url,
                session_ref=session_refs[case.case_id],
            ),
            retrieval_mode=retrieval_mode,
            fixtures=case.memory_fixtures,
            observed_entries=observed_entries[case.case_id],
            evaluated_at=evaluated_at,
        )
        failures, continuity = _assert_case(case=case, cold=cold, assisted=assisted)
        results.append(
            MemoryEffectivenessCaseResult(
                case_id=case.case_id,
                scenario=case.scenario,
                passed=not failures,
                failures=failures,
                cold=cold,
                assisted=assisted,
                session_continuity=continuity,
            )
        )
    failed_case_ids = [result.case_id for result in results if not result.passed]
    return MemoryEffectivenessReport(
        suite_name=suite.suite_name,
        retrieval_mode=retrieval_mode,
        status="passed" if not failed_case_ids else "failed",
        total_cases=len(results),
        passed_cases=len(results) - len(failed_case_ids),
        failed_case_ids=failed_case_ids,
        results=results,
    )


def write_memory_effectiveness_report(report: MemoryEffectivenessReport, output_path: Path) -> None:
    """Write a stable, newline-terminated M28.1 report artifact."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as stream:
        json.dump(report.model_dump(mode="json"), stream, indent=2, sort_keys=True)
        stream.write("\n")
