"""Antigravity CLI stream normalizer mapping `stream-json` events into AgentEvent."""

from __future__ import annotations

import logging
from typing import Any, Final, Literal

from sandbox.redact import SecretRedactor
from workers.agent_event import (
    AgentCompleted,
    AgentEvent,
    AgentFailed,
    AgentMessage,
    AgentProgress,
    AgentStarted,
    BudgetUpdated,
    FileChanged,
    ToolCompleted,
    ToolRequested,
)
from workers.agent_event_normalizer import safe_truncate_text
from workers.base import WorkerType
from workers.constants import (
    AGENT_EVENT_MAX_MESSAGE_CHARS,
    AGENT_EVENT_MAX_SUMMARY_CHARS,
    AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS,
)

logger = logging.getLogger(__name__)

ANTIGRAVITY_KNOWN_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "init",
        "start",
        "progress",
        "thinking",
        "message",
        "content",
        "tool_call",
        "tool_result",
        "file_edit",
        "file_changed",
        "usage",
        "token_count",
        "error",
        "fatal",
        "result",
        "complete",
    }
)


class AntigravityStreamNormalizer:
    """Normalizes raw Antigravity CLI stream-json lines into canonical AgentEvent instances."""

    def is_known_type(self, raw: dict[str, Any]) -> bool:
        event_type = raw.get("type") or raw.get("event")
        return isinstance(event_type, str) and event_type in ANTIGRAVITY_KNOWN_EVENT_TYPES

    def normalize(
        self,
        raw: dict[str, Any],
        *,
        sequence: int = 0,
        run_id: str = "",
        task_id: str | None = None,
        session_id: str | None = None,
        worker_type: WorkerType | None = "antigravity",
        redactor: SecretRedactor | None = None,
    ) -> AgentEvent | None:
        event_type = raw.get("type") or raw.get("event")
        if not isinstance(event_type, str) or event_type not in ANTIGRAVITY_KNOWN_EVENT_TYPES:
            return None

        base_kwargs = {
            "run_id": run_id,
            "sequence": sequence,
            "task_id": task_id,
            "session_id": session_id,
            "worker_type": worker_type,
            "provider_event_type": event_type,
        }

        try:
            return self._dispatch_event(event_type, raw, base_kwargs, redactor)
        except (ValueError, TypeError, KeyError) as exc:
            logger.debug("Failed to normalize Antigravity event (%s): %s", event_type, exc)
            return None

    def _dispatch_event(
        self,
        event_type: str,
        raw: dict[str, Any],
        base_kwargs: dict[str, Any],
        redactor: SecretRedactor | None,
    ) -> AgentEvent | None:
        if event_type in ("init", "start"):
            return AgentStarted(command=raw.get("command"), **base_kwargs)
        if event_type == "thinking":
            return AgentProgress(phase="thinking", message=None, **base_kwargs)
        if event_type == "progress":
            msg = safe_truncate_text(
                raw.get("message") or raw.get("text"),
                redactor=redactor,
                limit=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS,
            )
            return AgentProgress(phase=raw.get("phase"), message=msg, **base_kwargs)
        if event_type in ("message", "content"):
            return self._normalize_message(raw, base_kwargs, redactor)
        if event_type == "tool_call":
            return self._normalize_tool_call(raw, base_kwargs, redactor)
        if event_type == "tool_result":
            return self._normalize_tool_result(raw, base_kwargs, redactor)
        if event_type in ("file_edit", "file_changed"):
            return self._normalize_file_change(raw, base_kwargs)
        if event_type in ("usage", "token_count"):
            return self._normalize_usage(raw, base_kwargs)
        if event_type in ("error", "fatal"):
            return self._normalize_error(raw, base_kwargs, redactor)
        if event_type in ("result", "complete"):
            return self._normalize_complete(raw, base_kwargs, redactor)
        return None

    def _normalize_message(
        self, raw: dict[str, Any], base_kwargs: dict[str, Any], redactor: SecretRedactor | None
    ) -> AgentMessage:
        content = safe_truncate_text(
            raw.get("content") or raw.get("message") or raw.get("text") or "",
            redactor=redactor,
            limit=AGENT_EVENT_MAX_MESSAGE_CHARS,
        )
        role = raw.get("role")
        valid_role: Literal["assistant", "user", "system"] = (
            role if role in ("user", "system") else "assistant"
        )
        return AgentMessage(role=valid_role, content=content or "", **base_kwargs)

    def _normalize_tool_call(
        self, raw: dict[str, Any], base_kwargs: dict[str, Any], redactor: SecretRedactor | None
    ) -> ToolRequested | None:
        tool_name = raw.get("tool") or raw.get("name") or (raw.get("function") or {}).get("name")
        if not tool_name or not isinstance(tool_name, str):
            return None
        call_id = raw.get("call_id") or raw.get("id")
        raw_args = raw.get("arguments") or raw.get("input") or raw.get("args")
        input_summary = safe_truncate_text(
            raw_args, redactor=redactor, limit=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS
        )
        return ToolRequested(
            tool_name=tool_name,
            call_id=str(call_id) if call_id else None,
            input_summary=input_summary,
            **base_kwargs,
        )

    def _normalize_tool_result(
        self, raw: dict[str, Any], base_kwargs: dict[str, Any], redactor: SecretRedactor | None
    ) -> ToolCompleted:
        tool_name = (
            raw.get("tool")
            or raw.get("name")
            or (raw.get("function") or {}).get("name")
            or "unknown_tool"
        )
        call_id = raw.get("call_id") or raw.get("id")
        raw_output = raw.get("output") or raw.get("result") or raw.get("summary")
        output_summary = safe_truncate_text(
            raw_output, redactor=redactor, limit=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS
        )
        exit_code = raw.get("exit_code")
        duration = raw.get("duration") or raw.get("duration_seconds")
        return ToolCompleted(
            tool_name=str(tool_name),
            call_id=str(call_id) if call_id else None,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            output_summary=output_summary,
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
            **base_kwargs,
        )

    def _normalize_file_change(
        self, raw: dict[str, Any], base_kwargs: dict[str, Any]
    ) -> FileChanged | None:
        path = raw.get("path") or raw.get("file")
        if not path or not isinstance(path, str):
            return None
        kind = raw.get("change_kind")
        valid_kind: Literal["added", "modified", "deleted"] = (
            kind if kind in ("added", "deleted") else "modified"
        )
        return FileChanged(path=path[:500], change_kind=valid_kind, **base_kwargs)

    def _normalize_usage(self, raw: dict[str, Any], base_kwargs: dict[str, Any]) -> BudgetUpdated:
        tokens = (
            raw.get("tokens")
            or raw.get("tokens_used")
            or raw.get("total_tokens")
            or raw.get("count")
        )
        cost = raw.get("cost") or raw.get("cost_usd")
        ratio = raw.get("context_window_ratio")
        return BudgetUpdated(
            tokens_used=int(tokens) if isinstance(tokens, int | float) else None,
            cost_usd=float(cost) if isinstance(cost, int | float) else None,
            context_window_ratio=float(ratio) if isinstance(ratio, int | float) else None,
            **base_kwargs,
        )

    def _normalize_error(
        self, raw: dict[str, Any], base_kwargs: dict[str, Any], redactor: SecretRedactor | None
    ) -> AgentFailed:
        summary = (
            safe_truncate_text(
                raw.get("error") or raw.get("message") or "Fatal error",
                redactor=redactor,
                limit=AGENT_EVENT_MAX_SUMMARY_CHARS,
            )
            or "Fatal error"
        )
        kind = raw.get("failure_kind")
        exit_code = raw.get("exit_code")
        return AgentFailed(
            failure_summary=summary,
            failure_kind=str(kind) if kind else None,
            exit_code=int(exit_code) if isinstance(exit_code, int) else 1,
            **base_kwargs,
        )

    def _normalize_complete(
        self, raw: dict[str, Any], base_kwargs: dict[str, Any], redactor: SecretRedactor | None
    ) -> AgentCompleted:
        summary = (
            safe_truncate_text(
                raw.get("summary") or raw.get("result") or raw.get("message") or "Task completed",
                redactor=redactor,
                limit=AGENT_EVENT_MAX_SUMMARY_CHARS,
            )
            or "Task completed"
        )
        exit_code = raw.get("exit_code")
        return AgentCompleted(
            final_summary=summary,
            exit_code=int(exit_code) if isinstance(exit_code, int) else 0,
            **base_kwargs,
        )
