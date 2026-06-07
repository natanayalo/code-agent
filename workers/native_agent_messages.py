from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final, Literal

from apps.observability import (
    DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS,
    DEFAULT_FINAL_MESSAGE_READ_BUFFER,
)

_JSON_DECODER = json.JSONDecoder()
_FINAL_MESSAGE_FIELDS: Final = (
    "response",
    "error",
    "final_output",
    "summary",
    "message",
    "content",
    "text",
)

DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS = 1000
_STDOUT_FALLBACK_TRUNCATION_NOTE = "[stdout truncated for summary]\n"


logger = logging.getLogger(__name__)


def _detect_reason_code(
    *,
    status: Literal["success", "failure", "error"],
    timed_out: bool,
    exit_code: int | None,
    summary: str,
    stderr: str,
) -> tuple[str, str]:
    """Return stable reason_code/reason_detail for native run observability."""
    if timed_out:
        return "timeout", "command_timeout"
    if status == "success":
        return "ok", "completed"

    detail = summary.strip().lower()
    stderr_l = stderr.lower()
    if "auth method" in detail or "gemini_api_key" in stderr_l:
        return "auth_missing", "missing_auth_configuration"
    if "requires user confirmation" in detail:
        return "approval_blocked_noninteractive", "requires_user_confirmation"
    if "tool registry mismatch" in detail:
        return "tool_registry_mismatch", "tool_not_found"
    if "shell crash" in detail or "sandbox_infra" in detail:
        return "sandbox_infra_crash", "shell_crash_detected"
    if "could not start" in detail:
        return "process_start_failure", "process_could_not_start"
    if "failed while collecting artifacts" in detail:
        return "artifact_collection_failure", "artifact_collection_failed"
    if exit_code not in (None, 0):
        return "nonzero_exit", f"exit_code_{exit_code}"
    return "unknown_error", "unclassified_failure"


def _extract_final_message(raw_text: str) -> str | None:
    """Extract a meaningful final message from raw text or JSON."""
    candidate = raw_text.strip()
    if not candidate:
        return None

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return candidate

    if isinstance(payload, str):
        value = payload.strip()
        return value or None

    if isinstance(payload, dict):
        for field_name in _FINAL_MESSAGE_FIELDS:
            raw_value = payload.get(field_name)
            if raw_value is None:
                continue

            # Handle structured error payloads (parity with previous GeminiCliWorker logic)
            if field_name == "error" and isinstance(raw_value, dict):
                err_type = raw_value.get("type")
                err_msg = raw_value.get("message")
                if isinstance(err_type, str) and isinstance(err_msg, str):
                    return f"{err_type}: {err_msg}"
                if isinstance(err_msg, str):
                    return err_msg
                if isinstance(err_type, str):
                    return err_type

            if isinstance(raw_value, str):
                normalized = raw_value.strip()
                if normalized:
                    return normalized
            if isinstance(raw_value, dict):
                return json.dumps(raw_value)

    return candidate


def _normalize_stream_payload(payload: str | bytes | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


def _read_final_message(path: Path) -> str | None:
    """Read and parse the final message from a file."""
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        raw_payload = handle.read(DEFAULT_FINAL_MESSAGE_READ_BUFFER)
    truncated = len(raw_payload) > DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS
    raw_text = raw_payload[:DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS].strip()
    if not raw_text:
        return None

    extracted = _extract_final_message(raw_text)
    if extracted and truncated:
        return f"{extracted}\n\n[final message truncated for safety]"
    return extracted


def _stdout_fallback_final_message(stdout_text: str) -> str | None:
    """Extract final message from stdout tail, prioritizing JSON extraction from the end."""
    candidate = stdout_text.strip()
    if not candidate:
        return None

    # Limit search space to avoid parsing giant outputs
    search_limit = DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS
    is_truncated = len(candidate) > search_limit
    search_space = candidate[-search_limit:] if is_truncated else candidate

    # 1. Try parsing the search space as a whole (could be a full JSON response)
    extracted = _extract_final_message(search_space)
    if extracted and extracted != search_space:
        return extracted

    # 2. Try finding a JSON block in the search space (could be logs followed by JSON)
    # We iterate backwards and use raw_decode to find the last valid JSON object.
    pos = search_space.rfind("{")
    while pos != -1:
        try:
            _, end_idx = _JSON_DECODER.raw_decode(search_space[pos:])
            block = search_space[pos : pos + end_idx]
            extracted = _extract_final_message(block)
            if extracted and extracted != block:
                return extracted
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug("Failed to decode JSON block at position %d: %s", pos, e)
        pos = search_space.rfind("{", 0, pos)

    # 3. Fallback to raw text (with truncation note if applicable)
    if is_truncated:
        return f"{_STDOUT_FALLBACK_TRUNCATION_NOTE}{search_space}"
    return extracted or search_space
