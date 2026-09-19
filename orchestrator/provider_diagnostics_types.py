"""Types and data contracts for provider pre-dispatch diagnostics."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from workers.base import FailureKind

DEFAULT_PREFLIGHT_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_DOCKER_PROBE_TIMEOUT_SECONDS: Final[float] = 3.0
PROBE_CACHE_TTL_SECONDS: Final[float] = 15.0

DiagnosticStatus = Literal["ready", "unready", "unknown"]
DiagnosticCategory = Literal["credentials", "container_runtime", "cli_binary", "capacity"]
VerificationScope = Literal[
    "local_presence",
    "local_structure",
    "local_runtime",
    "remote_validity",
]
PreDispatchDecision = Literal[
    "preflight_passed",
    "preflight_rejected",
    "preflight_skipped",
    "preflight_error",
    "preflight_timeout",
    "preflight_budget_exhausted",
]


@dataclass(frozen=True)
class ProviderExecutionContext:
    """Authoritative execution parameters resolved for one worker dispatch."""

    provider: str
    worker_profile: str | None
    runtime_mode: str
    model: str | None
    reasoning_effort: str | None
    executable: str | None
    executor_image: str | None
    auth_mechanism: Literal["chatgpt_oauth", "api_key", "antigravity_oauth", "none"]
    credential_refs: tuple[str, ...]
    is_container_execution: bool
    task_id: str | None = None
    session_id: str | None = None
    attempt_count: int = 1
    logical_execution_key: str | None = None


class DiagnosticCheckResult(BaseModel):
    """Result of an individual prerequisite check."""

    model_config = ConfigDict(extra="forbid")

    name: str
    category: DiagnosticCategory
    status: DiagnosticStatus
    detail: str
    remediation: str | None = None
    verification_scope: VerificationScope
    blocking: bool


class ProviderPreDispatchReport(BaseModel):
    """Execution readiness report for a specific worker configuration."""

    model_config = ConfigDict(extra="forbid")

    report_id: str
    checked_at: datetime
    execution_environment_id: str
    logical_execution_key: str
    provider: str
    worker_profile: str | None = None
    runtime_mode: str
    model: str | None = None
    decision: PreDispatchDecision
    ready: bool
    execution_started: bool = False
    checks: list[DiagnosticCheckResult] = Field(default_factory=list)
    failure_kind: FailureKind | None = None
    next_action_hint: str | None = None
    reason_code: str | None = None
    summary: str | None = None


class SystemProviderDiagnosticsReport(BaseModel):
    """Operator-facing aggregate snapshot across supported providers."""

    model_config = ConfigDict(extra="forbid")

    checked_at: datetime
    expires_at: datetime
    target: str = "local"
    execution_environment_id: str
    providers: dict[str, ProviderPreDispatchReport]
    all_ready: bool
    required_providers_ready: bool


def compute_report_id(logical_execution_key: str) -> str:
    """Return a deterministic 32-character hex ID for preflight evidence."""
    return hashlib.sha256(f"preflight:v1:{logical_execution_key}".encode()).hexdigest()[:32]
