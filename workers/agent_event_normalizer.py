"""Provider stream normalizer protocol, config, and stream processing loop."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from sandbox.redact import SecretRedactor, redact_and_truncate_output
from workers.agent_event import (
    AgentCompleted,
    AgentEvent,
    AgentFailed,
    AgentStarted,
    FileChanged,
    assert_event_sequence_monotonic,
)
from workers.base import WorkerType
from workers.constants import (
    AGENT_EVENT_MAX_PER_RUN,
    AGENT_EVENT_MAX_SUMMARY_CHARS,
    AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS,
    AGENT_EVENT_RESERVED_LIFECYCLE,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormalizationConfig:
    """Settings controlling agent event stream normalization."""

    max_events_per_run: int = AGENT_EVENT_MAX_PER_RUN
    reserved_lifecycle_events: int = AGENT_EVENT_RESERVED_LIFECYCLE
    redact_text_fields: bool = True


@dataclass
class NormalizationStats:
    """Accounting metrics for one event normalization run."""

    total_raw: int = 0
    normalized: int = 0
    dropped_malformed: int = 0
    dropped_overflow: int = 0
    dropped_unknown: int = 0


@runtime_checkable
class ProviderStreamNormalizer(Protocol):
    """Protocol implemented by provider-specific stream adapters."""

    def normalize(
        self,
        raw: dict[str, Any],
        *,
        sequence: int = 0,
        run_id: str = "",
        task_id: str | None = None,
        session_id: str | None = None,
        worker_type: WorkerType | None = None,
        redactor: SecretRedactor | None = None,
    ) -> AgentEvent | list[AgentEvent] | None:
        """Normalize one provider record into an AgentEvent or list of events.

        Returns None if malformed or unmapped.
        """
        ...

    def is_known_type(self, raw: dict[str, Any]) -> bool:
        """Return True if the raw event type is recognized by this provider normalizer."""
        ...


def safe_truncate_text(
    val: Any,
    redactor: SecretRedactor | None = None,
    limit: int = AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS,
    *,
    redact: bool = True,
) -> str | None:
    """Redact and enforce bounded length on text fields."""
    if val is None:
        return None
    text = val if isinstance(val, str) else json.dumps(val)
    if not text:
        return ""
    if redact:
        effective_limit = max(10, limit - 60)
        text = redact_and_truncate_output(text, redactor=redactor, limit_chars=effective_limit)
    if len(text) > limit:
        text = text[:limit]
    return text


def _parse_raw_record(record: dict[str, Any] | str) -> tuple[dict[str, Any] | None, bool]:
    """Parse string or dict record. Returns (dict_or_none, is_malformed)."""
    if isinstance(record, dict):
        return record, False
    if isinstance(record, str):
        line = record.strip()
        if not line:
            return None, False
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                return parsed, False
            return None, True
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, True
    return None, True


def _resolve_failure_summary(
    last_failed: AgentFailed | None,
    *,
    execution_status: Literal["success", "failure", "error"] | None,
    execution_summary: str | None,
    default_exit_code: int | None,
    redactor: SecretRedactor | None = None,
) -> str:
    if (
        last_failed
        and last_failed.failure_summary
        and (execution_status == "success" or not execution_summary)
    ):
        raw_summary = last_failed.failure_summary
    else:
        raw_summary = (
            execution_summary
            or (
                last_failed.failure_summary if last_failed and last_failed.failure_summary else None
            )
            or (
                f"Process exited with code {default_exit_code}"
                if default_exit_code is not None
                else "Execution failed"
            )
        )
    return (
        safe_truncate_text(raw_summary, redactor=redactor, limit=AGENT_EVENT_MAX_SUMMARY_CHARS)
        or "Execution failed"
    )


def _reconcile_terminal_event(
    provider_terminals: Sequence[AgentCompleted | AgentFailed],
    *,
    default_exit_code: int | None,
    execution_status: Literal["success", "failure", "error"] | None = None,
    execution_summary: str | None = None,
    run_id: str,
    task_id: str | None,
    session_id: str | None,
    worker_type: WorkerType | None,
    redactor: SecretRedactor | None = None,
) -> AgentEvent:
    last_failed = next(
        (e for e in reversed(provider_terminals) if isinstance(e, AgentFailed)),
        None,
    )
    is_failed_run = (
        (default_exit_code is not None and default_exit_code != 0)
        or (execution_status is not None and execution_status != "success")
        or (last_failed is not None)
    )
    if is_failed_run:
        summary = _resolve_failure_summary(
            last_failed,
            execution_status=execution_status,
            execution_summary=execution_summary,
            default_exit_code=default_exit_code,
            redactor=redactor,
        )
        failure_kind = (
            last_failed.failure_kind
            if last_failed and last_failed.failure_kind
            else "process_error"
        )
        effective_exit_code = (
            default_exit_code
            if (default_exit_code is not None and default_exit_code != 0)
            else (
                last_failed.exit_code
                if (
                    last_failed and last_failed.exit_code is not None and last_failed.exit_code != 0
                )
                else 1
            )
        )
        return AgentFailed(
            run_id=run_id,
            sequence=0,
            task_id=task_id,
            session_id=session_id,
            worker_type=worker_type,
            failure_summary=summary,
            failure_kind=failure_kind,
            exit_code=effective_exit_code,
        )
    if provider_terminals:
        return provider_terminals[-1]
    summary = (
        safe_truncate_text(
            execution_summary or "Agent run completed.",
            redactor=redactor,
            limit=AGENT_EVENT_MAX_SUMMARY_CHARS,
        )
        or "Agent run completed."
    )
    return AgentCompleted(
        run_id=run_id,
        sequence=0,
        task_id=task_id,
        session_id=session_id,
        worker_type=worker_type,
        final_summary=summary,
        exit_code=0,
    )


def _append_bridged_files(
    events: list[AgentEvent],
    files_changed: list[str],
    *,
    run_id: str,
    task_id: str | None,
    session_id: str | None,
    worker_type: WorkerType | None,
    non_reserved_limit: int,
    max_events: int | None,
    stats: NormalizationStats | None,
) -> None:
    existing_paths = {e.path for e in events if isinstance(e, FileChanged)}
    current_non_lifecycle = sum(1 for e in events if not isinstance(e, AgentStarted))
    for path in files_changed:
        if path in existing_paths:
            continue
        if current_non_lifecycle >= non_reserved_limit or (
            max_events is not None and len(events) + 1 >= max_events
        ):
            if stats is not None:
                stats.dropped_overflow += 1
            continue
        events.append(
            FileChanged(
                run_id=run_id,
                sequence=0,
                task_id=task_id,
                session_id=session_id,
                worker_type=worker_type,
                path=path[:500],
                change_kind="modified",
            )
        )
        existing_paths.add(path)
        current_non_lifecycle += 1


def _inject_lifecycle_and_file_evidence(
    events: list[AgentEvent],
    *,
    run_id: str,
    task_id: str | None,
    session_id: str | None,
    worker_type: WorkerType | None,
    default_exit_code: int | None,
    files_changed: list[str] | None,
    execution_status: Literal["success", "failure", "error"] | None = None,
    execution_summary: str | None = None,
    non_reserved_limit: int = AGENT_EVENT_MAX_PER_RUN,
    max_events: int | None = None,
    stats: NormalizationStats | None = None,
    redactor: SecretRedactor | None = None,
) -> list[AgentEvent]:
    """Ensure AgentStarted, bridged FileChanged, and terminal events are present."""
    has_started = any(isinstance(e, AgentStarted) for e in events)
    if not has_started:
        events.insert(
            0,
            AgentStarted(
                run_id=run_id,
                sequence=0,
                task_id=task_id,
                session_id=session_id,
                worker_type=worker_type,
            ),
        )

    provider_terminals = [e for e in events if isinstance(e, AgentCompleted | AgentFailed)]
    events = [e for e in events if not isinstance(e, AgentCompleted | AgentFailed)]

    terminal_event = _reconcile_terminal_event(
        provider_terminals,
        default_exit_code=default_exit_code,
        execution_status=execution_status,
        execution_summary=execution_summary,
        run_id=run_id,
        task_id=task_id,
        session_id=session_id,
        worker_type=worker_type,
        redactor=redactor,
    )

    if files_changed:
        _append_bridged_files(
            events,
            files_changed,
            run_id=run_id,
            task_id=task_id,
            session_id=session_id,
            worker_type=worker_type,
            non_reserved_limit=non_reserved_limit,
            max_events=max_events,
            stats=stats,
        )

    events.append(terminal_event)
    resequenced = [
        ev.model_copy(update={"sequence": seq}) for seq, ev in enumerate(events, start=1)
    ]
    assert_event_sequence_monotonic(resequenced, strictly_consecutive=True)
    return resequenced


def _append_stream_event(
    ev: AgentEvent,
    events: list[AgentEvent],
    stats: NormalizationStats,
    *,
    non_reserved_limit: int,
    max_events: int,
) -> None:
    is_lifecycle = isinstance(ev, AgentCompleted | AgentFailed)
    limit = max_events if is_lifecycle else non_reserved_limit
    if len(events) >= limit:
        stats.dropped_overflow += 1
        return
    events.append(ev)
    stats.normalized += 1


def _normalize_and_append_record(
    raw_dict: dict[str, Any],
    normalizer: ProviderStreamNormalizer,
    events: list[AgentEvent],
    stats: NormalizationStats,
    *,
    cfg: NormalizationConfig,
    run_id: str,
    task_id: str | None,
    session_id: str | None,
    worker_type: WorkerType | None,
    redactor: SecretRedactor | None,
    non_reserved_limit: int,
) -> None:
    event_type = raw_dict.get("type") or raw_dict.get("event")
    if not event_type or not isinstance(event_type, str):
        stats.dropped_malformed += 1
        logger.debug("Dropped raw event missing string event_type: %s", raw_dict)
        return

    if hasattr(normalizer, "is_known_type") and not normalizer.is_known_type(raw_dict):
        stats.dropped_unknown += 1
        logger.debug("Dropped unknown provider event type: %s", event_type)
        return

    event = normalizer.normalize(
        raw_dict,
        sequence=len(events) + 1,
        run_id=run_id,
        task_id=task_id,
        session_id=session_id,
        worker_type=worker_type,
        redactor=redactor if cfg.redact_text_fields else None,
    )
    if event is None:
        stats.dropped_malformed += 1
        logger.debug("Normalizer rejected raw event of type %s", event_type)
        return

    produced = event if isinstance(event, list) else [event]
    for ev in produced:
        _append_stream_event(
            ev,
            events,
            stats,
            non_reserved_limit=non_reserved_limit,
            max_events=cfg.max_events_per_run,
        )


def normalize_provider_stream(
    records: Iterable[dict[str, Any] | str],
    normalizer: ProviderStreamNormalizer,
    config: NormalizationConfig | None = None,
    *,
    run_id: str,
    task_id: str | None = None,
    session_id: str | None = None,
    worker_type: WorkerType | None = None,
    redactor: SecretRedactor | None = None,
    default_exit_code: int | None = None,
    files_changed: list[str] | None = None,
    execution_status: Literal["success", "failure", "error"] | None = None,
    execution_summary: str | None = None,
) -> tuple[list[AgentEvent], NormalizationStats]:
    """Normalize raw provider event stream records into versioned AgentEvent objects."""
    cfg = config or NormalizationConfig()
    stats = NormalizationStats()
    events: list[AgentEvent] = []
    non_reserved_limit = max(0, cfg.max_events_per_run - cfg.reserved_lifecycle_events)

    for record in records:
        raw_dict, is_malformed = _parse_raw_record(record)
        if raw_dict is None and not is_malformed:
            continue
        stats.total_raw += 1
        if is_malformed or raw_dict is None:
            stats.dropped_malformed += 1
            logger.debug("Dropped malformed raw event record")
            continue

        _normalize_and_append_record(
            raw_dict,
            normalizer,
            events,
            stats,
            cfg=cfg,
            run_id=run_id,
            task_id=task_id,
            session_id=session_id,
            worker_type=worker_type,
            redactor=redactor,
            non_reserved_limit=non_reserved_limit,
        )

    final_events = _inject_lifecycle_and_file_evidence(
        events,
        run_id=run_id,
        task_id=task_id,
        session_id=session_id,
        worker_type=worker_type,
        default_exit_code=default_exit_code,
        files_changed=files_changed,
        execution_status=execution_status,
        execution_summary=execution_summary,
        non_reserved_limit=non_reserved_limit,
        max_events=cfg.max_events_per_run,
        stats=stats,
        redactor=redactor if cfg.redact_text_fields else None,
    )
    return final_events, stats
