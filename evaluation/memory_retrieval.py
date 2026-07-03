"""Deterministic evaluation helpers for skeptical-memory retrieval quality."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from orchestrator.graph import build_load_memory_node
from orchestrator.state import OrchestratorState
from repositories import PersonalMemoryRepository, ProjectMemoryRepository, session_scope

_DEFAULT_SUITE_PATH = Path(__file__).with_name("memory_retrieval_suite.json")
_RETRIEVAL_MODE = "full_text"


@dataclass(frozen=True, slots=True)
class MemorySeedEntry:
    """One skeptical-memory row to seed before retrieval evaluation."""

    memory_key: str
    value: dict[str, Any]
    source: str | None = None
    confidence: float = 1.0
    scope: str | None = None
    requires_verification: bool = True


@dataclass(frozen=True, slots=True)
class MemoryRetrievalCase:
    """One retrieval query and its expected memory-key outcomes."""

    case_id: str
    task_text: str
    expected_personal_keys: tuple[str, ...]
    expected_project_keys: tuple[str, ...]
    known_semantic_gap_personal_keys: tuple[str, ...] = ()
    known_semantic_gap_project_keys: tuple[str, ...] = ()
    repo_url: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryRetrievalSuite:
    """A deterministic skeptical-memory retrieval evaluation suite."""

    suite_name: str
    repo_url: str
    personal_memory: tuple[MemorySeedEntry, ...]
    project_memory: tuple[MemorySeedEntry, ...]
    cases: tuple[MemoryRetrievalCase, ...]


@dataclass(frozen=True, slots=True)
class MemoryLoadedPayload:
    """Subset of the MEMORY_LOADED payload included in each case report."""

    retrieval_mode: str | None
    search_query: str | None
    search_limit: int | None
    personal_keys: tuple[str, ...]
    project_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "personal_keys": sorted(self.personal_keys),
            "project_keys": sorted(self.project_keys),
            "retrieval_mode": self.retrieval_mode,
            "search_limit": self.search_limit,
            "search_query": self.search_query,
        }


@dataclass(frozen=True, slots=True)
class MemoryRetrievalCaseResult:
    """Per-case retrieval result with regression and semantic-gap misses split."""

    case_id: str
    passed: bool
    expected_personal_keys: tuple[str, ...]
    expected_project_keys: tuple[str, ...]
    actual_personal_keys: tuple[str, ...]
    actual_project_keys: tuple[str, ...]
    missing_personal_keys: tuple[str, ...]
    missing_project_keys: tuple[str, ...]
    known_semantic_gap_personal_misses: tuple[str, ...]
    known_semantic_gap_project_misses: tuple[str, ...]
    memory_loaded_payload: MemoryLoadedPayload

    def to_dict(self) -> dict[str, Any]:
        return {
            "actual_personal_keys": sorted(self.actual_personal_keys),
            "actual_project_keys": sorted(self.actual_project_keys),
            "case_id": self.case_id,
            "expected_personal_keys": sorted(self.expected_personal_keys),
            "expected_project_keys": sorted(self.expected_project_keys),
            "known_semantic_gap_personal_misses": sorted(self.known_semantic_gap_personal_misses),
            "known_semantic_gap_project_misses": sorted(self.known_semantic_gap_project_misses),
            "memory_loaded_payload": self.memory_loaded_payload.to_dict(),
            "missing_personal_keys": sorted(self.missing_personal_keys),
            "missing_project_keys": sorted(self.missing_project_keys),
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class MemoryRetrievalReport:
    """Aggregate report for a deterministic memory-retrieval evaluation run."""

    suite_name: str
    retrieval_mode: str
    total_cases: int
    cases_with_full_recall: int
    cases_with_regression_misses: int
    cases_with_known_semantic_gap_misses: int
    recall: float | None
    regression_misses: tuple[str, ...]
    known_semantic_gap_misses: tuple[str, ...]
    results: tuple[MemoryRetrievalCaseResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cases_with_full_recall": self.cases_with_full_recall,
            "cases_with_known_semantic_gap_misses": (self.cases_with_known_semantic_gap_misses),
            "cases_with_regression_misses": self.cases_with_regression_misses,
            "known_semantic_gap_misses": sorted(self.known_semantic_gap_misses),
            "recall": self.recall,
            "regression_misses": sorted(self.regression_misses),
            "results": [result.to_dict() for result in sorted(self.results, key=_case_id)],
            "retrieval_mode": self.retrieval_mode,
            "suite_name": self.suite_name,
            "total_cases": self.total_cases,
        }


class _MemorySeedPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    memory_key: str
    value: dict[str, Any]
    source: str | None = None
    confidence: float = 1.0
    scope: str | None = None
    requires_verification: bool = True

    @field_validator("memory_key")
    @classmethod
    def _require_non_empty_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string.")
        return value


class _RetrievalCasePayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    case_id: str
    task_text: str
    repo_url: str | None = None
    expected_personal_keys: list[str] = Field(default_factory=list)
    expected_project_keys: list[str] = Field(default_factory=list)
    known_semantic_gap_personal_keys: list[str] = Field(default_factory=list)
    known_semantic_gap_project_keys: list[str] = Field(default_factory=list)

    @field_validator("case_id", "task_text")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string.")
        return value


class _RetrievalSuitePayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    suite_name: str
    repo_url: str
    personal_memory: list[_MemorySeedPayload] = Field(default_factory=list)
    project_memory: list[_MemorySeedPayload] = Field(default_factory=list)
    cases: list[_RetrievalCasePayload]

    @field_validator("suite_name", "repo_url")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string.")
        return value


_SUITE_ADAPTER = TypeAdapter(_RetrievalSuitePayload)


def _summarize_validation_error(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_input=False)
    details: list[str] = []
    for error in errors:
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "invalid value"))
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        details.append(f"{location}: {message}" if location else message)
    return "; ".join(details) if details else "invalid payload"


def _sorted_tuple(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _case_id(result: MemoryRetrievalCaseResult) -> str:
    return result.case_id


def _seed_entry_from_payload(payload: _MemorySeedPayload) -> MemorySeedEntry:
    return MemorySeedEntry(
        memory_key=payload.memory_key,
        value=dict(payload.value),
        source=payload.source,
        confidence=payload.confidence,
        scope=payload.scope,
        requires_verification=payload.requires_verification,
    )


def _case_from_payload(payload: _RetrievalCasePayload) -> MemoryRetrievalCase:
    return MemoryRetrievalCase(
        case_id=payload.case_id,
        task_text=payload.task_text,
        repo_url=payload.repo_url,
        expected_personal_keys=_sorted_tuple(payload.expected_personal_keys),
        expected_project_keys=_sorted_tuple(payload.expected_project_keys),
        known_semantic_gap_personal_keys=_sorted_tuple(payload.known_semantic_gap_personal_keys),
        known_semantic_gap_project_keys=_sorted_tuple(payload.known_semantic_gap_project_keys),
    )


def load_memory_retrieval_suite(path: Path | None = None) -> MemoryRetrievalSuite:
    """Load and validate a deterministic memory-retrieval suite JSON file."""
    suite_path = path or _DEFAULT_SUITE_PATH
    with suite_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    try:
        parsed = _SUITE_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise ValueError(
            f"Memory retrieval suite validation failed: {_summarize_validation_error(exc)}"
        ) from exc

    seen_case_ids: set[str] = set()
    cases: list[MemoryRetrievalCase] = []
    for raw_case in parsed.cases:
        if raw_case.case_id in seen_case_ids:
            raise ValueError(
                f"Duplicate case_id found in memory retrieval suite: {raw_case.case_id}"
            )
        seen_case_ids.add(raw_case.case_id)
        cases.append(_case_from_payload(raw_case))

    return MemoryRetrievalSuite(
        suite_name=parsed.suite_name,
        repo_url=parsed.repo_url,
        personal_memory=tuple(_seed_entry_from_payload(entry) for entry in parsed.personal_memory),
        project_memory=tuple(_seed_entry_from_payload(entry) for entry in parsed.project_memory),
        cases=tuple(sorted(cases, key=lambda case: case.case_id)),
    )


def _seed_memory_suite(session_factory: Any, suite: MemoryRetrievalSuite) -> None:
    with session_scope(session_factory) as session:
        personal_repo = PersonalMemoryRepository(session)
        project_repo = ProjectMemoryRepository(session)
        for entry in suite.personal_memory:
            personal_repo.upsert(
                memory_key=entry.memory_key,
                value=entry.value,
                source=entry.source,
                confidence=entry.confidence,
                scope=entry.scope,
                requires_verification=entry.requires_verification,
            )
        for entry in suite.project_memory:
            project_repo.upsert(
                repo_url=suite.repo_url,
                memory_key=entry.memory_key,
                value=entry.value,
                source=entry.source,
                confidence=entry.confidence,
                scope=entry.scope,
                requires_verification=entry.requires_verification,
            )


def _memory_loaded_payload_from_result(result: dict[str, Any]) -> MemoryLoadedPayload:
    payload: dict[str, Any] = {}
    for event in result.get("timeline_events", []):
        event_payload = getattr(event, "payload", None)
        if isinstance(event_payload, dict) and event_payload.get("retrieval_mode") is not None:
            payload = event_payload
            break
    return MemoryLoadedPayload(
        retrieval_mode=payload.get("retrieval_mode"),
        search_query=payload.get("search_query"),
        search_limit=payload.get("search_limit"),
        personal_keys=_sorted_tuple(payload.get("personal_keys", [])),
        project_keys=_sorted_tuple(payload.get("project_keys", [])),
    )


def _case_result(
    *,
    case: MemoryRetrievalCase,
    payload: MemoryLoadedPayload,
) -> MemoryRetrievalCaseResult:
    actual_personal = set(payload.personal_keys)
    actual_project = set(payload.project_keys)
    known_personal = set(case.known_semantic_gap_personal_keys)
    known_project = set(case.known_semantic_gap_project_keys)
    expected_personal = set(case.expected_personal_keys)
    expected_project = set(case.expected_project_keys)

    missing_personal = expected_personal - known_personal - actual_personal
    missing_project = expected_project - known_project - actual_project
    known_personal_misses = known_personal - actual_personal
    known_project_misses = known_project - actual_project

    return MemoryRetrievalCaseResult(
        case_id=case.case_id,
        passed=not missing_personal and not missing_project,
        expected_personal_keys=_sorted_tuple(case.expected_personal_keys),
        expected_project_keys=_sorted_tuple(case.expected_project_keys),
        actual_personal_keys=_sorted_tuple(payload.personal_keys),
        actual_project_keys=_sorted_tuple(payload.project_keys),
        missing_personal_keys=tuple(sorted(missing_personal)),
        missing_project_keys=tuple(sorted(missing_project)),
        known_semantic_gap_personal_misses=tuple(sorted(known_personal_misses)),
        known_semantic_gap_project_misses=tuple(sorted(known_project_misses)),
        memory_loaded_payload=payload,
    )


def _qualified_keys(*, case_id: str, category: str, keys: tuple[str, ...]) -> list[str]:
    return [f"{case_id}:{category}:{key}" for key in keys]


def _compute_recall(results: tuple[MemoryRetrievalCaseResult, ...]) -> float | None:
    expected_count = 0
    retrieved_count = 0
    for result in results:
        expected_personal = set(result.expected_personal_keys) - set(
            result.known_semantic_gap_personal_misses
        )
        expected_project = set(result.expected_project_keys) - set(
            result.known_semantic_gap_project_misses
        )
        expected_count += len(expected_personal) + len(expected_project)
        retrieved_count += len(expected_personal & set(result.actual_personal_keys))
        retrieved_count += len(expected_project & set(result.actual_project_keys))
    if expected_count == 0:
        return None
    return retrieved_count / expected_count


def _build_report(
    *,
    suite: MemoryRetrievalSuite,
    results: tuple[MemoryRetrievalCaseResult, ...],
) -> MemoryRetrievalReport:
    regression_misses: list[str] = []
    known_gap_misses: list[str] = []
    for result in results:
        regression_misses.extend(
            _qualified_keys(
                case_id=result.case_id,
                category="personal",
                keys=result.missing_personal_keys,
            )
        )
        regression_misses.extend(
            _qualified_keys(
                case_id=result.case_id,
                category="project",
                keys=result.missing_project_keys,
            )
        )
        known_gap_misses.extend(
            _qualified_keys(
                case_id=result.case_id,
                category="personal",
                keys=result.known_semantic_gap_personal_misses,
            )
        )
        known_gap_misses.extend(
            _qualified_keys(
                case_id=result.case_id,
                category="project",
                keys=result.known_semantic_gap_project_misses,
            )
        )

    return MemoryRetrievalReport(
        suite_name=suite.suite_name,
        retrieval_mode=_RETRIEVAL_MODE,
        total_cases=len(results),
        cases_with_full_recall=sum(
            1
            for result in results
            if not result.missing_personal_keys
            and not result.missing_project_keys
            and not result.known_semantic_gap_personal_misses
            and not result.known_semantic_gap_project_misses
        ),
        cases_with_regression_misses=sum(
            1 for result in results if result.missing_personal_keys or result.missing_project_keys
        ),
        cases_with_known_semantic_gap_misses=sum(
            1
            for result in results
            if result.known_semantic_gap_personal_misses or result.known_semantic_gap_project_misses
        ),
        recall=_compute_recall(results),
        regression_misses=tuple(sorted(regression_misses)),
        known_semantic_gap_misses=tuple(sorted(known_gap_misses)),
        results=tuple(sorted(results, key=_case_id)),
    )


def evaluate_memory_retrieval(
    *,
    suite: MemoryRetrievalSuite,
    session_factory: Any,
    search_limit: int = 20,
) -> MemoryRetrievalReport:
    """Evaluate the current load_memory retrieval path against a memory suite."""
    _seed_memory_suite(session_factory, suite)
    load_memory_node = build_load_memory_node(session_factory, search_limit=search_limit)
    results: list[MemoryRetrievalCaseResult] = []
    for case in sorted(suite.cases, key=lambda item: item.case_id):
        state = OrchestratorState(
            task={
                "task_text": case.task_text,
                "repo_url": case.repo_url or suite.repo_url,
                "branch": "master",
            }
        )
        node_result = load_memory_node(state)
        payload = _memory_loaded_payload_from_result(node_result)
        results.append(_case_result(case=case, payload=payload))
    return _build_report(suite=suite, results=tuple(results))


def write_memory_retrieval_report(
    report: MemoryRetrievalReport,
    output_path: Path,
) -> None:
    """Write a deterministic, newline-terminated retrieval evaluation report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report.to_dict(), file, indent=2, sort_keys=True)
        file.write("\n")
