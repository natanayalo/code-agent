from __future__ import annotations

import json
import logging
import re
from typing import Any, Final, Literal

_JSON_DECODER = json.JSONDecoder()
_FENCED_JSON_BLOCK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"```(?:json)?\s*(?P<body>.*?)```",
    re.IGNORECASE | re.DOTALL,
)
_LLM_WRAPPER_PAYLOAD_KEYS: Final[tuple[str, ...]] = ("response", "content", "summary")
_LLM_WRAPPER_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {"session_id", "stats", "models", "tools", "files"}
)
_TELEMETRY_ONLY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "api",
        "byName",
        "cached",
        "candidates",
        "files",
        "input",
        "models",
        "prompt",
        "roles",
        "total",
    }
)


logger = logging.getLogger(__name__)


def _split_llm_output_and_metadata(raw_output: str) -> tuple[Any, dict[str, Any] | None]:
    """Return (payload_for_output_value, wrapper_metadata) for CLI JSON envelopes."""
    text = raw_output.strip()
    if not text:
        return raw_output, None

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw_output, None

    if not isinstance(parsed, dict):
        return parsed, None

    parsed_keys = set(parsed)
    payload_key = next(
        (
            key
            for key in _LLM_WRAPPER_PAYLOAD_KEYS
            if key in parsed
            and (key == "response" or bool(parsed_keys.intersection(_LLM_WRAPPER_METADATA_KEYS)))
        ),
        None,
    )
    if payload_key is None:
        return parsed, None

    payload = parsed.get(payload_key)
    if isinstance(payload, str):
        candidate = payload.strip()
        if candidate:
            try:
                payload = json.loads(candidate)
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = payload
    metadata = {k: v for k, v in parsed.items() if k != payload_key}
    return payload, metadata or None


def _schema_property_names(response_schema: dict[str, Any] | None) -> frozenset[str]:
    if not response_schema:
        return frozenset()
    properties = response_schema.get("properties")
    if not isinstance(properties, dict):
        return frozenset()
    return frozenset(str(key) for key in properties)


def _json_payload_rejection_reason(
    payload: dict[str, Any],
    *,
    response_schema: dict[str, Any] | None,
    response_format: Literal["text", "json"],  # type: ignore[name-defined]
) -> str | None:
    keys = set(payload)
    if keys and keys <= _TELEMETRY_ONLY_KEYS:
        return "telemetry_only"

    schema_keys = _schema_property_names(response_schema)
    if response_format == "json" and schema_keys and not keys.intersection(schema_keys):
        return "schema_key_mismatch"

    return None


def _parse_json_dict(text: str) -> dict[str, Any] | None:
    try:
        candidate = json.loads(text.strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return candidate if isinstance(candidate, dict) else None


def _parse_json_dict_from_value(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    return _parse_json_dict(value)


def _iter_fenced_json_dicts(text: str) -> list[dict[str, Any]]:
    return [
        payload
        for match in _FENCED_JSON_BLOCK_PATTERN.finditer(text)
        if (payload := _parse_json_dict(match.group("body"))) is not None
    ]


def _iter_embedded_json_dicts(text: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    pos = text.find("{")
    while pos != -1:
        try:
            candidate, end_idx = _JSON_DECODER.raw_decode(text[pos:])
        except (json.JSONDecodeError, ValueError):
            pos = text.find("{", pos + 1)
            continue
        if isinstance(candidate, dict):
            payloads.append(candidate)
        pos = text.find("{", pos + max(end_idx, 1))
    return payloads


def _select_json_payload(
    candidates: list[tuple[str, dict[str, Any]]],
    *,
    response_schema: dict[str, Any] | None,
    response_format: Literal["text", "json"],  # type: ignore[name-defined]
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    rejected_reason: str | None = None
    for source, payload in candidates:
        rejection_reason = _json_payload_rejection_reason(
            payload,
            response_schema=response_schema,
            response_format=response_format,
        )
        if rejection_reason is None:
            return payload, source, None
        rejected_reason = rejected_reason or rejection_reason
    return None, None, rejected_reason


def _extract_business_json_payload(
    *,
    final_message: str | None,
    stdout_text: str,
    response_format: Literal["text", "json"],  # type: ignore[name-defined]
    response_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Extract model business JSON while rejecting CLI telemetry envelopes."""
    candidates: list[tuple[str, dict[str, Any]]] = []

    if final_message:
        if payload := _parse_json_dict(final_message):
            candidates.append(("final_message", payload))

    wrapper_payload = _parse_json_dict(stdout_text)
    if wrapper_payload is not None:
        for key in _LLM_WRAPPER_PAYLOAD_KEYS:
            if key not in wrapper_payload:
                continue
            if payload := _parse_json_dict_from_value(wrapper_payload[key]):
                candidates.append((f"stdout_wrapper.{key}", payload))

    for source_text_name, source_text in (
        ("final_message", final_message or ""),
        ("stdout", stdout_text),
    ):
        if not source_text:
            continue
        candidates.extend(
            (f"{source_text_name}.fenced_json", payload)
            for payload in reversed(_iter_fenced_json_dicts(source_text))
        )

    for source_text_name, source_text in (
        ("final_message", final_message or ""),
        ("stdout", stdout_text),
    ):
        if not source_text:
            continue
        candidates.extend(
            (f"{source_text_name}.embedded_json", payload)
            for payload in reversed(_iter_embedded_json_dicts(source_text))
        )

    return _select_json_payload(
        candidates,
        response_schema=response_schema,
        response_format=response_format,
    )
