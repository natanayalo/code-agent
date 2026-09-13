"""Codex CLI stream normalizer mapping `--json` events into AgentEvent."""

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

CODEX_KNOWN_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "thread.started",
        "turn.started",
        "item.started",
        "item.updated",
        "item.completed",
        "turn.completed",
        "turn.failed",
        "session_created",
        "message_delta",
        "reasoning",
        "message_complete",
        "assistant_message",
        "function_call_begin",
        "function_call_end",
        "function_output",
        "file_edit",
        "local_shell",
        "usage_delta",
        "usage",
        "error",
        "task_error",
        "session_complete",
        "task_complete",
    }
)


class CodexStreamNormalizer:
    """Normalizes raw Codex CLI JSON stream lines into canonical AgentEvent instances."""

    def is_known_type(self, raw: dict[str, Any]) -> bool:
        event_type = raw.get("type") or raw.get("event")
        return isinstance(event_type, str) and event_type in CODEX_KNOWN_EVENT_TYPES

    def normalize(
        self,
        raw: dict[str, Any],
        *,
        sequence: int = 0,
        run_id: str = "",
        task_id: str | None = None,
        session_id: str | None = None,
        worker_type: WorkerType | None = "codex",
        redactor: SecretRedactor | None = None,
    ) -> AgentEvent | list[AgentEvent] | None:
        event_type = raw.get("type") or raw.get("event")
        if not isinstance(event_type, str) or event_type not in CODEX_KNOWN_EVENT_TYPES:
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
            logger.debug("Failed to normalize Codex event (%s): %s", event_type, exc)
            return None

    def _dispatch_event(
        self,
        event_type: str,
        raw: dict[str, Any],
        base_kwargs: dict[str, Any],
        redactor: SecretRedactor | None,
    ) -> AgentEvent | list[AgentEvent] | None:
        if event_type in ("session_created", "thread.started"):
            return AgentStarted(command=raw.get("command"), **base_kwargs)
        if event_type == "turn.started":
            return AgentProgress(phase="turn_started", message=None, **base_kwargs)
        if event_type in ("item.started", "item.updated", "item.completed"):
            return self._normalize_item(event_type, raw, base_kwargs, redactor)
        if event_type == "turn.completed":
            usage = raw.get("usage")
            if isinstance(usage, dict):
                return self._normalize_usage(usage, base_kwargs)
            return self._normalize_complete(raw, base_kwargs, redactor)
        if event_type == "turn.failed":
            return self._normalize_error(raw, base_kwargs, redactor)
        if event_type == "message_delta":
            msg = safe_truncate_text(
                raw.get("delta") or raw.get("message") or raw.get("text"),
                redactor=redactor,
                limit=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS,
            )
            return AgentProgress(phase=raw.get("phase") or "generating", message=msg, **base_kwargs)
        if event_type == "reasoning":
            return AgentProgress(phase="reasoning", message=None, **base_kwargs)
        if event_type in ("message_complete", "assistant_message"):
            return self._normalize_message(raw, base_kwargs, redactor)
        if event_type == "function_call_begin":
            return self._normalize_tool_call(raw, base_kwargs, redactor)
        if event_type in ("function_call_end", "function_output"):
            return self._normalize_tool_completed(raw, base_kwargs, redactor)
        if event_type in ("file_edit", "local_shell"):
            return self._normalize_file_change(raw, base_kwargs)
        if event_type in ("usage_delta", "usage"):
            return self._normalize_usage(raw, base_kwargs)
        if event_type in ("error", "task_error"):
            return self._normalize_error(raw, base_kwargs, redactor)
        if event_type in ("session_complete", "task_complete"):
            return self._normalize_complete(raw, base_kwargs, redactor)
        return None

    def _normalize_command_item(
        self,
        event_type: str,
        item: dict[str, Any],
        base_kwargs: dict[str, Any],
        redactor: SecretRedactor | None,
    ) -> AgentEvent | None:
        tool_name = item.get("tool") or item.get("name") or "execute_bash"
        call_id = item.get("id") or item.get("call_id")
        if event_type == "item.started":
            input_cmd = item.get("command") or item.get("input") or item.get("arguments")
            input_summary = safe_truncate_text(
                input_cmd, redactor=redactor, limit=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS
            )
            return ToolRequested(
                tool_name=str(tool_name),
                call_id=str(call_id) if call_id else None,
                input_summary=input_summary,
                **base_kwargs,
            )
        if event_type == "item.updated":
            input_cmd = item.get("command") or item.get("input") or item.get("arguments")
            msg = safe_truncate_text(
                input_cmd, redactor=redactor, limit=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS
            )
            return AgentProgress(phase="executing", message=msg, **base_kwargs)

        raw_out = (
            item.get("aggregated_output")
            or item.get("output")
            or item.get("stdout")
            or item.get("result")
            or item.get("summary")
        )
        output_summary = safe_truncate_text(
            raw_out, redactor=redactor, limit=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS
        )
        exit_code = item.get("exit_code")
        duration = item.get("duration_seconds") or item.get("duration")
        return ToolCompleted(
            tool_name=str(tool_name),
            call_id=str(call_id) if call_id else None,
            exit_code=int(exit_code) if isinstance(exit_code, int) else None,
            output_summary=output_summary,
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
            **base_kwargs,
        )

    def _normalize_item(
        self,
        event_type: str,
        raw: dict[str, Any],
        base_kwargs: dict[str, Any],
        redactor: SecretRedactor | None,
    ) -> AgentEvent | list[AgentEvent] | None:
        raw_item = raw.get("item")
        item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else raw
        item_type = item.get("type") or item.get("item_type") or ""

        if item_type in ("command_execution", "exec_command", "shell") or "command" in item:
            return self._normalize_command_item(event_type, item, base_kwargs, redactor)

        if (
            item_type in ("file_edit", "file_change", "file_modify")
            or "changes" in item
            or "path" in item
        ):
            return self._normalize_file_change(item, base_kwargs)

        if item_type == "web_search":
            query = item.get("query") or item.get("search_query") or item.get("input") or ""
            msg = safe_truncate_text(
                query, redactor=redactor, limit=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS
            )
            return AgentProgress(phase="search", message=msg or None, **base_kwargs)

        if item_type == "todo_list":
            todos = item.get("items") or item.get("todos") or []
            total = len(todos) if isinstance(todos, list) else 0
            completed = (
                sum(1 for t in todos if isinstance(t, dict) and t.get("completed"))
                if isinstance(todos, list)
                else 0
            )
            msg = f"{completed}/{total} tasks completed" if total > 0 else "todo list updated"
            return AgentProgress(phase="plan", message=msg, **base_kwargs)

        if item_type == "error":
            err_msg = item.get("message") or item.get("error") or "Codex item error"
            msg = safe_truncate_text(
                err_msg, redactor=redactor, limit=AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS
            )
            return AgentProgress(phase="error", message=msg or "Codex item error", **base_kwargs)

        if item_type == "reasoning":
            return AgentProgress(phase="reasoning", message=None, **base_kwargs)

        if item_type in ("message", "agent_message", "assistant_message"):
            if event_type == "item.completed":
                return self._normalize_message(item, base_kwargs, redactor)
            return AgentProgress(phase="generating", message=None, **base_kwargs)

        if item_type in ("tool_call", "function_call", "custom_tool") or "tool" in item:
            if event_type == "item.started":
                return self._normalize_tool_call(item, base_kwargs, redactor)
            return self._normalize_tool_completed(item, base_kwargs, redactor)

        return None

    def _normalize_message(
        self, raw: dict[str, Any], base_kwargs: dict[str, Any], redactor: SecretRedactor | None
    ) -> AgentMessage | None:
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

    def _normalize_tool_completed(
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
        duration = raw.get("duration_seconds") or raw.get("duration")
        return ToolCompleted(
            tool_name=str(tool_name),
            call_id=str(call_id) if call_id else None,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            output_summary=output_summary,
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
            **base_kwargs,
        )

    def _map_change_kind(self, kind: Any) -> Literal["added", "modified", "deleted"]:
        if kind in ("add", "added"):
            return "added"
        if kind in ("delete", "deleted"):
            return "deleted"
        return "modified"

    def _normalize_file_change(
        self, raw: dict[str, Any], base_kwargs: dict[str, Any]
    ) -> FileChanged | list[AgentEvent] | None:
        raw_changes = raw.get("changes")
        if isinstance(raw_changes, list) and raw_changes:
            events: list[AgentEvent] = []
            for change in raw_changes:
                if not isinstance(change, dict):
                    continue
                path = change.get("path") or change.get("file")
                if not path or not isinstance(path, str):
                    continue
                kind = self._map_change_kind(change.get("kind") or change.get("change_kind"))
                events.append(FileChanged(path=path[:500], change_kind=kind, **base_kwargs))
            if events:
                return events

        path = raw.get("path") or raw.get("file")
        if not path or not isinstance(path, str):
            return None
        kind = self._map_change_kind(raw.get("change_kind") or raw.get("kind"))
        return FileChanged(path=path[:500], change_kind=kind, **base_kwargs)

    def _normalize_usage(self, raw: dict[str, Any], base_kwargs: dict[str, Any]) -> BudgetUpdated:
        tokens = raw.get("tokens") or raw.get("tokens_used") or raw.get("total_tokens")
        if tokens is None:
            input_tokens = raw.get("input_tokens")
            output_tokens = raw.get("output_tokens")
            if input_tokens is not None or output_tokens is not None:
                tokens = (int(input_tokens) if isinstance(input_tokens, int | float) else 0) + (
                    int(output_tokens) if isinstance(output_tokens, int | float) else 0
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
        raw_error = (
            raw.get("error") or raw.get("message") or raw.get("failure_summary") or "Task error"
        )
        summary = (
            safe_truncate_text(
                raw_error,
                redactor=redactor,
                limit=AGENT_EVENT_MAX_SUMMARY_CHARS,
            )
            or "Task error"
        )
        kind = raw.get("failure_kind") or raw.get("kind")
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
                raw.get("summary")
                or raw.get("final_summary")
                or raw.get("message")
                or "Task completed",
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
