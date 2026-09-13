"""Provider stream normalizer protocol, config, and stream processing loop."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

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
    ) -> AgentEvent | None:
        """Normalize one provider record into an AgentEvent or return None if malformed/unmapped."""
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


def _reconcile_terminal_event(
    provider_terminals: Sequence[AgentCompleted | AgentFailed],
    *,
    default_exit_code: int | None,
    run_id: str,
    task_id: str | None,
    session_id: str | None,
    worker_type: WorkerType | None,
) -> AgentEvent:
    if default_exit_code is not None and default_exit_code != 0:
        last_failed = next(
            (e for e in reversed(provider_terminals) if isinstance(e, AgentFailed)),
            None,
        )
        summary = (
            last_failed.failure_summary
            if last_failed and last_failed.failure_summary
            else f"Process exited with code {default_exit_code}"
        )
        failure_kind = (
            last_failed.failure_kind
            if last_failed and last_failed.failure_kind
            else "process_error"
        )
        return AgentFailed(
            run_id=run_id,
            sequence=0,
            task_id=task_id,
            session_id=session_id,
            worker_type=worker_type,
            failure_summary=summary,
            failure_kind=failure_kind,
            exit_code=default_exit_code,
        )
    if provider_terminals:
        return provider_terminals[-1]
    return AgentCompleted(
        run_id=run_id,
        sequence=0,
        task_id=task_id,
        session_id=session_id,
        worker_type=worker_type,
        final_summary="Agent run completed.",
        exit_code=0,
    )


def _inject_lifecycle_and_file_evidence(
    events: list[AgentEvent],
    *,
    run_id: str,
    task_id: str | None,
    session_id: str | None,
    worker_type: WorkerType | None,
    default_exit_code: int | None,
    files_changed: list[str] | None,
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
        run_id=run_id,
        task_id=task_id,
        session_id=session_id,
        worker_type=worker_type,
    )

    if files_changed:
        existing_paths = {e.path for e in events if isinstance(e, FileChanged)}
        for path in files_changed:
            if path not in existing_paths:
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

    events.append(terminal_event)
    resequenced = [
        ev.model_copy(update={"sequence": seq}) for seq, ev in enumerate(events, start=1)
    ]
    assert_event_sequence_monotonic(resequenced, strictly_consecutive=True)
    return resequenced


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

        event_type = raw_dict.get("type") or raw_dict.get("event")
        if not event_type or not isinstance(event_type, str):
            stats.dropped_malformed += 1
            logger.debug("Dropped raw event missing string event_type: %s", raw_dict)
            continue

        if hasattr(normalizer, "is_known_type") and not normalizer.is_known_type(raw_dict):
            stats.dropped_unknown += 1
            logger.debug("Dropped unknown provider event type: %s", event_type)
            continue

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
            continue

        is_lifecycle = isinstance(event, AgentCompleted | AgentFailed)
        if not is_lifecycle and len(events) >= non_reserved_limit:
            stats.dropped_overflow += 1
            continue
        if is_lifecycle and len(events) >= cfg.max_events_per_run:
            stats.dropped_overflow += 1
            continue

        events.append(event)
        stats.normalized += 1

    final_events = _inject_lifecycle_and_file_evidence(
        events,
        run_id=run_id,
        task_id=task_id,
        session_id=session_id,
        worker_type=worker_type,
        default_exit_code=default_exit_code,
        files_changed=files_changed,
    )
    return final_events, stats
