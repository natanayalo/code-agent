"""Policy helpers for execution-path orchestration."""

from __future__ import annotations

import copy
import ipaddress
import logging
import socket
from collections.abc import Mapping
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from db.enums import TaskStatus, WorkerRunStatus, WorkerType
from orchestrator.state import OrchestratorState
from tools.numeric import coerce_non_negative_int_like, coerce_positive_int_like

logger = logging.getLogger(__name__)

CALLBACK_RESOLUTION_TIMEOUT_SECONDS = 2.0
CALLBACK_DNS_EXECUTOR_MAX_WORKERS = 4
_callback_dns_executor: ThreadPoolExecutor | None = None
_callback_dns_executor_lock = Lock()
INTERACTIVE_EXECUTION_MODE = "interactive"
UNATTENDED_EXECUTION_MODE = "unattended"
VALID_EXECUTION_MODES = frozenset({INTERACTIVE_EXECUTION_MODE, UNATTENDED_EXECUTION_MODE})

DEFAULT_EXECUTION_BUDGETS: dict[str, dict[str, int]] = {
    INTERACTIVE_EXECUTION_MODE: {
        "max_iterations": 8,
        "worker_timeout_seconds": 600,
        "max_tool_calls": 24,
        "max_shell_commands": 24,
        "max_retries": 2,
    },
    UNATTENDED_EXECUTION_MODE: {
        "max_iterations": 5,
        "worker_timeout_seconds": 300,
        "max_tool_calls": 12,
        "max_shell_commands": 12,
        "max_retries": 1,
    },
}
GLOBAL_BUDGET_CAPS: dict[str, int] = {
    "max_iterations": 20,
    "worker_timeout_seconds": 900,
    "max_minutes": 15,
    "orchestrator_timeout_seconds": 930,
    "command_timeout_seconds": 300,
    "max_tool_calls": 100,
    "max_shell_commands": 100,
    "max_retries": 10,
    "max_verifier_passes": 5,
    "max_observation_characters": 12000,
}
SCOUT_BUDGET_CAPS: dict[str, Any] = {
    "execution_mode": UNATTENDED_EXECUTION_MODE,
    "max_iterations": 3,
    "worker_timeout_seconds": 180,
    "max_tool_calls": 8,
    "max_shell_commands": 8,
    "max_retries": 0,
}
NON_NEGATIVE_BUDGET_KEYS = frozenset(
    {"max_retries", "max_verifier_passes", "max_tool_calls", "max_shell_commands"}
)
NON_NEGATIVE_DEFAULT_BUDGET_KEYS = frozenset(
    {"max_retries", "max_tool_calls", "max_shell_commands"}
)


def _resolve_execution_mode(
    *,
    channel: str,
    constraints: Mapping[str, Any],
    budget: Mapping[str, Any],
) -> str:
    """Resolve execution mode with explicit overrides before channel defaults."""
    candidates = (constraints.get("execution_mode"), budget.get("execution_mode"))
    for candidate in candidates:
        if isinstance(candidate, str):
            normalized = candidate.strip().lower()
            if normalized in VALID_EXECUTION_MODES:
                return normalized
    normalized_channel = channel.strip().lower()
    return (
        INTERACTIVE_EXECUTION_MODE
        if normalized_channel == "telegram"
        else UNATTENDED_EXECUTION_MODE
    )


def _apply_execution_budget_policy(
    *,
    channel: str,
    constraints: Mapping[str, Any],
    budget: Mapping[str, Any],
) -> dict[str, Any]:
    """Return an effective runtime budget with mode defaults and global hard caps."""
    execution_mode = _resolve_execution_mode(
        channel=channel, constraints=constraints, budget=budget
    )
    effective_budget: dict[str, Any] = dict(budget)
    effective_budget["execution_mode"] = execution_mode

    for key, default_value in DEFAULT_EXECUTION_BUDGETS[execution_mode].items():
        if (
            key == "worker_timeout_seconds"
            and coerce_positive_int_like(effective_budget.get("worker_timeout_seconds")) is None
            and coerce_positive_int_like(effective_budget.get("max_minutes")) is not None
        ):
            continue
        coercer = (
            coerce_non_negative_int_like
            if key in NON_NEGATIVE_DEFAULT_BUDGET_KEYS
            else coerce_positive_int_like
        )
        if coercer(effective_budget.get(key)) is None:
            effective_budget[key] = default_value

    for key, cap in GLOBAL_BUDGET_CAPS.items():
        coercer = (
            coerce_non_negative_int_like
            if key in NON_NEGATIVE_BUDGET_KEYS
            else coerce_positive_int_like
        )
        coerced_value = coercer(effective_budget.get(key))
        if coerced_value is not None:
            effective_budget[key] = min(coerced_value, cap)
        elif key in effective_budget:
            effective_budget.pop(key, None)

    return effective_budget


def normalize_scout_submission(
    constraints: dict[str, Any] | None, budget: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Enforce Scout mode constraints and clamp budget caps."""
    safe_constraints = dict(constraints or {})
    safe_budget = dict(budget or {})

    is_scout = safe_constraints.get("task_type") == "scout"
    if not is_scout:
        return safe_constraints, safe_budget

    normalized_constraints = dict(safe_constraints)
    normalized_constraints["read_only"] = True

    normalized_budget = dict(safe_budget)
    normalized_budget["execution_mode"] = SCOUT_BUDGET_CAPS["execution_mode"]

    for key, cap in SCOUT_BUDGET_CAPS.items():
        if key == "execution_mode":
            continue
        val = normalized_budget.get(key)
        if val is None:
            normalized_budget[key] = cap
        else:
            try:
                coerced_val = int(float(val))
            except (ValueError, TypeError):
                raise ValueError(f"Invalid budget configuration for {key}: {val}")

            if coerced_val < 0:
                raise ValueError(f"Budget value for {key} cannot be negative")
            normalized_budget[key] = min(coerced_val, cap)

    return normalized_constraints, normalized_budget


def _is_unsafe_callback_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether a resolved callback destination is local-only or otherwise unsafe."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped

    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _lookup_callback_hostname_records(hostname: str, port: int) -> list[tuple]:
    """Resolve a hostname through the system resolver using TCP-oriented address hints."""
    return socket.getaddrinfo(
        hostname,
        port,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )


def _get_callback_dns_executor() -> ThreadPoolExecutor:
    """Return the shared executor used for bounded callback DNS resolution."""
    global _callback_dns_executor
    with _callback_dns_executor_lock:
        if _callback_dns_executor is None:
            _callback_dns_executor = ThreadPoolExecutor(
                max_workers=CALLBACK_DNS_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="callback-dns",
            )
        return _callback_dns_executor


def shutdown_callback_dns_executor() -> None:
    """Shut down the shared callback DNS executor for app/test teardown."""
    global _callback_dns_executor
    with _callback_dns_executor_lock:
        executor = _callback_dns_executor
        _callback_dns_executor = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def _heartbeat_interval_seconds(*, lease_seconds: int) -> float:
    """Choose a lease heartbeat cadence that scales with lease duration."""
    bounded_lease = max(1, lease_seconds)
    return max(1.0, min(10.0, bounded_lease / 3.0))


def _resolve_callback_hostname(
    hostname: str,
    *,
    port: int,
    timeout_seconds: float = CALLBACK_RESOLUTION_TIMEOUT_SECONDS,
) -> list[str]:
    """Resolve a callback hostname into concrete destination IP addresses."""
    executor = _get_callback_dns_executor()
    try:
        future = executor.submit(_lookup_callback_hostname_records, hostname, port)
        records = future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        future.cancel()
        raise ValueError("callback_url hostname resolution timed out.") from exc
    except socket.gaierror as exc:
        raise ValueError("callback_url hostname could not be resolved.") from exc
    except FutureCancelledError as exc:
        raise ValueError("callback_url hostname resolution was cancelled.") from exc

    resolved_addresses: list[str] = []
    for family, _, _, _, sockaddr in records:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        if not sockaddr:
            continue
        candidate = sockaddr[0].strip()
        if candidate:
            resolved_addresses.append(candidate)

    if not resolved_addresses:
        raise ValueError("callback_url hostname did not resolve to any addresses.")
    return resolved_addresses


def validate_callback_url(value: str | None) -> str | None:
    """Reject callback targets that are malformed or obviously unsafe for outbound POSTs."""
    if value is None:
        return None

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("callback_url must use http or https.")
    if not parsed.netloc or parsed.hostname is None:
        raise ValueError("callback_url must be an absolute URL with a hostname.")

    hostname = parsed.hostname.strip().lower()
    if hostname == "localhost":
        raise ValueError("callback_url must not target localhost.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("callback_url must include a valid port.") from exc
    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    try:
        ipaddress.ip_address(hostname)
        resolved_addresses = [hostname]
    except ValueError:
        resolved_addresses = _resolve_callback_hostname(hostname, port=port)

    for resolved_address in resolved_addresses:
        host_ip = ipaddress.ip_address(resolved_address)
        if _is_unsafe_callback_address(host_ip):
            raise ValueError("callback_url must not target a private or local address.")
    return value


def _validate_callback_url(value: str | None) -> str | None:
    """Backward-compatible alias for callers/tests that still reference the private name."""
    return validate_callback_url(value)


def _deep_merge(
    target: dict[str, Any],
    source: dict[str, Any],
    *,
    reserved_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Recursively merge two dictionaries, returning a new result."""
    merged = copy.deepcopy(target)

    def strip_reserved(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: strip_reserved(v)
                for k, v in obj.items()
                if not (reserved_keys and k in reserved_keys)
            }
        if isinstance(obj, list):
            return [strip_reserved(v) for v in obj]
        return copy.deepcopy(obj)

    def merge_in_place(base: dict[str, Any], overrides: dict[str, Any]) -> None:
        for key, value in overrides.items():
            if reserved_keys and key in reserved_keys:
                continue
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                merge_in_place(base[key], value)
            else:
                base[key] = strip_reserved(value)

    merge_in_place(merged, source)
    return merged


def _sanitize_submission_constraints(
    constraints: Mapping[str, Any],
    *,
    reserved_keys: frozenset[str],
) -> dict[str, Any]:
    """Drop reserved control-plane keys that callers must not set directly."""
    sanitized = dict(constraints)
    for key in reserved_keys:
        sanitized.pop(key, None)
    return sanitized


def _task_status_from_result(state: OrchestratorState) -> TaskStatus:
    """Map the final orchestrator result into a persisted task status."""
    if state.approval.required and state.approval.status == "pending":
        return TaskStatus.PENDING
    if state.result is None:
        return TaskStatus.FAILED
    if state.result.status == "success":
        return TaskStatus.COMPLETED
    return TaskStatus.FAILED


def _worker_run_status_from_result(state: OrchestratorState) -> WorkerRunStatus:
    """Map the final worker result into a persisted worker-run status."""
    if state.result is None:
        return WorkerRunStatus.ERROR
    if state.result.status == "success":
        return WorkerRunStatus.SUCCESS
    if state.result.status == "failure":
        return WorkerRunStatus.FAILURE
    return WorkerRunStatus.ERROR


def _worker_type_for_persistence(state: OrchestratorState) -> WorkerType:
    """Choose a persisted worker type even when dispatch metadata is incomplete."""
    if state.dispatch.worker_type is not None:
        return WorkerType(state.dispatch.worker_type)

    if state.route.chosen_worker is not None:
        logger.warning(
            "Persisting worker run with route fallback because dispatch worker type is missing.",
            extra={"route_worker": state.route.chosen_worker},
        )
        return WorkerType(state.route.chosen_worker)

    logger.warning("Persisting worker run with codex default because worker type is missing.")
    return WorkerType.CODEX
