"""Deterministic synthesis for scored improvement proposals."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from orchestrator.reflection import (
    EffortScore,
    FrictionReport,
    HitlNeed,
    ImprovementSuggestion,
    LayerImpact,
    RiskScore,
    ValueScore,
)

_CRITICAL_VALUE_FAILURE_KINDS = frozenset(
    {
        "timeout",
        "sandbox_infra",
        "test_regression",
        "infra_verifier_unavailable",
    }
)
_HIGH_RISK_FAILURE_KINDS = frozenset(
    {
        "sandbox_infra",
        "infra_verifier_unavailable",
        "permission_denied",
        "risky_command",
    }
)
_GENERIC_TITLE_PREFIXES = frozenset(
    {
        "error",
        "exception",
        "failure",
        "failed",
        "failed to execute command",
        "warning",
    }
)


@dataclass(frozen=True)
class ImprovementSuggestionDraft:
    """A scored suggestion plus the stable source-friction fingerprint."""

    suggestion: ImprovementSuggestion
    fingerprint: str


def build_improvement_suggestion_draft(
    report: FrictionReport,
    *,
    task_id: str,
    attempt_count: int,
    failure_kind: str | None = None,
    retry_context: bool = False,
) -> ImprovementSuggestionDraft:
    """Build a deterministic scored improvement suggestion from one friction report."""
    description = _display_description(report)
    effective_failure_kind = _effective_failure_kind(report, failure_kind)
    layer_impact = _layer_impact(report)
    risk = _risk_score(report, layer_impact, effective_failure_kind, description)
    suggestion = ImprovementSuggestion(
        title=_title_for_report(report, description, effective_failure_kind),
        description=_suggestion_description(report, description),
        value=_value_score(
            report,
            description,
            effective_failure_kind,
            attempt_count=attempt_count,
            retry_context=retry_context,
        ),
        effort=_effort_score(report, layer_impact),
        risk=risk,
        layer_impact=layer_impact,
        validation_path=_validation_path(layer_impact),
        hitl_need=_hitl_need(risk),
    )
    return ImprovementSuggestionDraft(
        suggestion=suggestion,
        fingerprint=compute_friction_fingerprint(report, task_id=task_id),
    )


def compute_friction_fingerprint(report: FrictionReport, *, task_id: str) -> str:
    """Return the stable fingerprint used to dedupe reflection proposals."""
    source = report.source or "other"
    impact = report.impact or "unknown"
    safe_desc = (report.description or "").strip()
    fingerprint_input = f"{task_id}:{source}:{impact}:{safe_desc}".encode()
    return hashlib.sha256(fingerprint_input).hexdigest()


def _effective_failure_kind(report: FrictionReport, failure_kind: str | None) -> str | None:
    if failure_kind:
        return failure_kind
    context = report.context if isinstance(report.context, dict) else {}
    context_failure_kind = context.get("failure_kind")
    return context_failure_kind if isinstance(context_failure_kind, str) else None


def _display_description(report: FrictionReport) -> str:
    description = (report.description or "").strip()
    if description:
        return description
    source = (report.source or "other").replace("_", " ")
    return f"{source} friction was reported without a detailed description."


def _source_label(report: FrictionReport) -> str:
    return (report.source or "other").replace("_", " ")


def _layer_impact(report: FrictionReport) -> LayerImpact:
    source = report.source or "other"
    if source == "sandbox":
        return "sandbox"
    if source == "orchestrator":
        return "orchestrator"
    if source in {"tooling", "instructions"}:
        return "worker"
    return "other"


def _value_score(
    report: FrictionReport,
    description: str,
    failure_kind: str | None,
    *,
    attempt_count: int,
    retry_context: bool,
) -> ValueScore:
    desc_lower = description.lower()
    if (
        report.impact == "blocked"
        or retry_context
        or attempt_count > 1
        or failure_kind in _CRITICAL_VALUE_FAILURE_KINDS
        or "timeout" in desc_lower
        or ("test" in desc_lower and "fail" in desc_lower)
        or "sandbox" in desc_lower
    ):
        return "high"
    if report.impact in {"slowed_down", "required_workaround"}:
        return "medium"
    return "low"


def _effort_score(report: FrictionReport, layer_impact: LayerImpact) -> EffortScore:
    if report.source in {"instructions", "tooling"}:
        return "small"
    if layer_impact == "sandbox":
        return "large"
    if layer_impact in {"orchestrator", "worker"}:
        return "medium"
    return "medium"


def _risk_score(
    report: FrictionReport,
    layer_impact: LayerImpact,
    failure_kind: str | None,
    description: str,
) -> RiskScore:
    desc_lower = description.lower()
    if (
        report.source == "sandbox"
        or layer_impact == "sandbox"
        or failure_kind in _HIGH_RISK_FAILURE_KINDS
        or "permission" in desc_lower
        or "sandbox" in desc_lower
    ):
        return "high"
    if report.source == "instructions":
        return "low"
    if layer_impact in {"orchestrator", "worker"}:
        return "medium"
    return "medium"


def _hitl_need(risk: RiskScore) -> HitlNeed:
    if risk == "high":
        return "required"
    if risk == "medium":
        return "optional"
    return "none"


def _validation_path(layer_impact: LayerImpact) -> str:
    if layer_impact == "sandbox":
        return (
            "Run sandbox runner integration tests and a vertical-slice e2e smoke covering "
            "the affected execution path."
        )
    if layer_impact == "orchestrator":
        return (
            "Run targeted orchestrator unit/integration tests plus vertical-slice e2e for "
            "the affected state transition."
        )
    if layer_impact == "worker":
        return (
            "Run targeted worker/orchestrator tests for the friction path and a focused "
            "dispatch smoke if worker behavior changes."
        )
    return "Run targeted regression tests for the reported friction and verify the task path."


def _title_for_report(
    report: FrictionReport,
    description: str,
    failure_kind: str | None,
) -> str:
    desc_lower = description.lower()
    if failure_kind == "timeout" or "timeout" in desc_lower:
        return "Improve native timeout handling"
    if failure_kind == "sandbox_infra" or "infra crash" in desc_lower:
        return "Harden sandbox infrastructure recovery"
    if "verification failed" in desc_lower:
        return "Improve verifier failure recovery"
    if failure_kind == "test_regression" or ("test" in desc_lower and "fail" in desc_lower):
        return "Reduce repeated test failure friction"
    if not (report.description or "").strip():
        return f"Improve {_source_label(report)} friction handling"

    cleaned_desc = description
    if ": " in cleaned_desc:
        prefix, suffix = cleaned_desc.split(": ", maxsplit=1)
        if prefix.strip().lower() in _GENERIC_TITLE_PREFIXES:
            cleaned_desc = suffix.strip()
    first_part = cleaned_desc[:80]
    first_part = " ".join(first_part.split()).rstrip(".:,;")
    if len(first_part) < 3:
        return f"Improve {_source_label(report)} friction handling"
    return f"Improve {first_part} handling"


def _suggestion_description(report: FrictionReport, description: str) -> str:
    source = _source_label(report)
    if not (report.description or "").strip():
        return f"Reduce recurring {source} friction observed during task execution."
    return f"Reduce recurring {source} friction observed during task execution: {description}"


__all__ = [
    "ImprovementSuggestionDraft",
    "build_improvement_suggestion_draft",
    "compute_friction_fingerprint",
]
