"""Formatting, markdown rendering, and public allowlist validation for M29 reports."""

from __future__ import annotations

import json
import re
from typing import Any

from evaluation.provider_reliability_models import (
    ProviderReliabilityReport,
)

SAFE_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SAFE_CLASS_PATTERN = re.compile(r"^[a-z0-9_]+$")
UNSAFE_VALUE_SUBSTRINGS = ("://", "/Users/", "/home/", "/root/", "/tmp/")

ALLOWED_KEYS_BY_LEVEL: dict[str, frozenset[str]] = {
    "root": frozenset(
        {
            "schema_version",
            "generated_at",
            "status",
            "policy",
            "profile_coverage",
            "exclusions",
            "evidence_cells",
            "recommendations",
        }
    ),
    "policy": frozenset(
        {
            "schema_version",
            "lookback_days",
            "min_samples",
            "confidence_level",
            "as_of",
            "window_start_at",
            "window_end_at",
            "enabled_profiles",
            "expected_groups",
            "evidence_scope",
            "expected_execution_identities",
        }
    ),
    "execution_identity": frozenset(
        {
            "provider",
            "model",
            "reasoning_effort",
        }
    ),
    "profile_coverage": frozenset(
        {
            "profile",
            "mutation_mode",
            "has_evidence",
            "sample_size",
            "is_eligible",
        }
    ),
    "exclusions": frozenset(
        {
            "total_tasks_scanned",
            "included_tasks_count",
            "excluded_tasks_count",
            "by_reason",
        }
    ),
    "cell": frozenset(
        {
            "task_class",
            "profile",
            "mutation_mode",
            "sample_size",
            "oldest_evidence_timestamp",
            "newest_evidence_timestamp",
            "accepted_count",
            "accepted_task_rate",
            "accepted_task_rate_ci",
            "failure_count",
            "failure_rate",
            "stage_outcome_rates",
            "typed_failures",
            "repairs",
            "interventions",
            "manual_overrides_count",
            "manual_override_rate",
            "terminal_latency",
            "budget_field_coverage",
            "is_eligible",
            "insufficiency_reasons",
        }
    ),
    "ci": frozenset({"lower", "center", "upper"}),
    "stages": frozenset(
        {
            "dispatch_rate",
            "execution_success_rate",
            "verification_pass_rate",
            "review_pass_rate",
            "delivery_pass_rate",
        }
    ),
    "repairs": frozenset(
        {
            "verifier_repairs_count",
            "review_repairs_count",
            "total_repaired_tasks",
            "repair_rate",
        }
    ),
    "interventions": frozenset(
        {
            "human_interventions_count",
            "clarification_questions_count",
            "approvals_count",
            "intervention_rate",
        }
    ),
    "latency": frozenset(
        {
            "median_seconds",
            "mean_seconds",
            "min_seconds",
            "max_seconds",
            "p90_seconds",
        }
    ),
    "budget": frozenset({"budget_reported_count", "budget_coverage_rate"}),
    "recommendation": frozenset(
        {
            "task_class",
            "mutation_mode",
            "recommended_profile",
            "rankings",
            "fallback_reason",
        }
    ),
    "ranking": frozenset(
        {
            "profile",
            "is_eligible",
            "accepted_rate_wilson_lower",
            "median_latency_seconds",
            "rank",
            "insufficiency_reasons",
            "sample_size",
            "accepted_count",
            "oldest_evidence_timestamp",
            "newest_evidence_timestamp",
            "evidence_age_days",
        }
    ),
}


VALID_TASK_CLASSES: frozenset[str] = frozenset(
    {"docs", "bugfix", "feature", "refactor", "investigation", "review_fix", "maintenance", "scout"}
)
VALID_MUTATION_MODES: frozenset[str] = frozenset({"mutation", "read_only"})
VALID_FAILURE_KINDS: frozenset[str] = frozenset(
    {
        # Canonical worker failure kinds (workers.base.FailureKind)
        "compile",
        "test",
        "tool_runtime",
        "sandbox_infra",
        "timeout",
        "budget_exceeded",
        "permission_denied",
        "context_window",
        "provider_error",
        "provider_auth",
        "incomplete_delivery",
        "test_regression",
        "scope_mismatch",
        "infra_verifier_unavailable",
        "risky_command",
        "worker_failure",
        "interaction",
        "read_only_violation",
        # Verification failure kinds and legacy taxonomy markers
        "compile_error",
        "test_failure",
        "syntax_error",
        "syntax",
        "lint",
        "type_check",
        # Extractor synthesized failure kinds
        "task_error",
        "unknown",
    }
)
VALID_EXCLUSION_REASONS: frozenset[str] = frozenset(
    {
        "cancelled",
        "incomplete",
        "non_temporal_runtime",
        "non_native_agent_mode",
        "outside_window",
        "malformed_missing_task_spec",
        "malformed_missing_profile",
        "malformed_inconsistent_timeline",
        "mixed_profile_execution",
        "incompatible_profile_mode",
        "malformed_missing_mode",
        "unknown_execution_identity",
        "mixed_execution_identity",
        "execution_identity_mismatch",
        "evaluation_smoke",
    }
)


def _validate_domain_value(key: str, val: Any) -> None:
    """Validate string values against domain boundaries and reject unsafe substrings."""
    if isinstance(val, str):
        for sub in UNSAFE_VALUE_SUBSTRINGS:
            if sub in val:
                raise ValueError(f"Unsafe substring '{sub}' detected in field {key}: {val}")
        if key == "profile" and not SAFE_IDENTIFIER_PATTERN.match(val):
            raise ValueError(f"Invalid profile identifier format: {val}")
        if key in ("provider", "model") and not SAFE_IDENTIFIER_PATTERN.match(val):
            raise ValueError(f"Invalid {key} identifier format: {val}")
        if key == "reasoning_effort" and val not in ("low", "medium", "high"):
            raise ValueError(f"Invalid reasoning_effort value: {val}")
        if key == "evidence_scope" and val not in ("operational", "current_execution_cohort"):
            raise ValueError(f"Invalid evidence_scope domain value: {val}")
        if key == "task_class":
            if not SAFE_CLASS_PATTERN.match(val) or val not in VALID_TASK_CLASSES:
                raise ValueError(f"Invalid task_class domain value: {val}")
        if key == "mutation_mode":
            if not SAFE_CLASS_PATTERN.match(val) or val not in VALID_MUTATION_MODES:
                raise ValueError(f"Invalid mutation_mode domain value: {val}")


def _validate_dict_keys(data: dict[str, Any], allowed: frozenset[str], path: str) -> None:
    """Ensure dictionary keys strictly match the defined allowlist."""
    unknown = set(data.keys()) - allowed
    if unknown:
        keys_list = sorted(unknown)
        raise ValueError(
            f"Forbidden key detected: unauthorized keys in public report at '{path}': {keys_list}"
        )


def _validate_cell_structure(cell: dict[str, Any], path: str) -> None:
    """Validate keys and nested structures of an evidence cell."""
    _validate_dict_keys(cell, ALLOWED_KEYS_BY_LEVEL["cell"], path)
    if "accepted_task_rate_ci" in cell:
        _validate_dict_keys(
            cell["accepted_task_rate_ci"], ALLOWED_KEYS_BY_LEVEL["ci"], f"{path}.ci"
        )
    if "stage_outcome_rates" in cell:
        _validate_dict_keys(
            cell["stage_outcome_rates"], ALLOWED_KEYS_BY_LEVEL["stages"], f"{path}.stages"
        )
    if "repairs" in cell:
        _validate_dict_keys(cell["repairs"], ALLOWED_KEYS_BY_LEVEL["repairs"], f"{path}.repairs")
    if "interventions" in cell:
        _validate_dict_keys(
            cell["interventions"], ALLOWED_KEYS_BY_LEVEL["interventions"], f"{path}.interventions"
        )
    if "terminal_latency" in cell:
        _validate_dict_keys(
            cell["terminal_latency"], ALLOWED_KEYS_BY_LEVEL["latency"], f"{path}.latency"
        )
    if "budget_field_coverage" in cell:
        _validate_dict_keys(
            cell["budget_field_coverage"], ALLOWED_KEYS_BY_LEVEL["budget"], f"{path}.budget"
        )
    for fk in cell.get("typed_failures", {}):
        if not SAFE_CLASS_PATTERN.match(fk) or fk not in VALID_FAILURE_KINDS:
            raise ValueError(f"Invalid typed failure key in {path}: {fk}")


def assert_sanitized_report(payload: dict[str, Any] | str) -> None:
    """Validate that report conforms strictly to public allowlists and safe domains."""
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid report JSON payload: {exc}") from exc
    else:
        data = payload

    _validate_dict_keys(data, ALLOWED_KEYS_BY_LEVEL["root"], "root")
    if "policy" in data:
        _validate_dict_keys(data["policy"], ALLOWED_KEYS_BY_LEVEL["policy"], "policy")
        for prof, ident in data["policy"].get("expected_execution_identities", {}).items():
            if not SAFE_IDENTIFIER_PATTERN.match(prof):
                raise ValueError(f"Invalid profile in expected_execution_identities: {prof}")
            if isinstance(ident, dict):
                _validate_dict_keys(
                    ident,
                    ALLOWED_KEYS_BY_LEVEL["execution_identity"],
                    f"policy.expected_execution_identities[{prof}]",
                )
    if "exclusions" in data:
        _validate_dict_keys(data["exclusions"], ALLOWED_KEYS_BY_LEVEL["exclusions"], "exclusions")
        for rk in data["exclusions"].get("by_reason", {}):
            if not SAFE_CLASS_PATTERN.match(rk) or rk not in VALID_EXCLUSION_REASONS:
                raise ValueError(f"Invalid exclusion reason key: {rk}")

    for idx, cov in enumerate(data.get("profile_coverage", [])):
        _validate_dict_keys(
            cov, ALLOWED_KEYS_BY_LEVEL["profile_coverage"], f"profile_coverage[{idx}]"
        )

    for idx, cell in enumerate(data.get("evidence_cells", [])):
        _validate_cell_structure(cell, f"evidence_cells[{idx}]")

    for idx, rec in enumerate(data.get("recommendations", [])):
        _validate_dict_keys(rec, ALLOWED_KEYS_BY_LEVEL["recommendation"], f"recommendations[{idx}]")
        for c_idx, cand in enumerate(rec.get("rankings", [])):
            _validate_dict_keys(
                cand, ALLOWED_KEYS_BY_LEVEL["ranking"], f"recommendations[{idx}].rankings[{c_idx}]"
            )

    def _scan_values(item: Any, key_context: str = "") -> None:
        if isinstance(item, str):
            _validate_domain_value(key_context, item)
        elif isinstance(item, dict):
            for k, v in item.items():
                _scan_values(v, k)
        elif isinstance(item, list):
            for entry in item:
                _scan_values(entry, key_context)

    _scan_values(data)


def escape_markdown(val: Any) -> str:
    """Escape pipe characters and strip newlines for safe Markdown table cells."""
    if val is None:
        return "N/A"
    text_val = str(val).replace("\n", " ").replace("\r", " ").replace("|", "\\|").strip()
    return text_val


def render_json_report(report: ProviderReliabilityReport) -> str:
    """Render the report as a deterministic, sorted, sanitized JSON string."""
    dumped = report.model_dump(mode="json")
    assert_sanitized_report(dumped)
    return json.dumps(dumped, indent=2, sort_keys=True) + "\n"


def _render_header(report: ProviderReliabilityReport) -> list[str]:
    """Render report title, status, and policy metadata."""
    p = report.policy
    return [
        "# M29 Provider Reliability Advisory Report",
        "",
        f"- **Status**: `{report.status}`",
        f"- **Evidence Scope**: `{p.evidence_scope}`",
        f"- **Generated At**: `{report.generated_at.isoformat()}`",
        f"- **Evidence Window**: `{p.window_start_at.isoformat()}` to "
        f"`{p.window_end_at.isoformat()}` ({p.lookback_days} days)",
        f"- **Minimum Samples Per Cell**: `{p.min_samples}`",
        f"- **Confidence Level**: `{int(p.confidence_level * 100)}%` (Wilson score interval)",
        "",
    ]


def _render_exclusions(report: ProviderReliabilityReport) -> list[str]:
    """Render task accounting and exclusions table."""
    ex = report.exclusions
    lines = [
        "## 1. Evidence Accounting & Exclusions",
        "",
        f"- **Total Tasks Scanned**: {ex.total_tasks_scanned}",
        f"- **Included In Evidence**: {ex.included_tasks_count}",
        f"- **Excluded Tasks**: {ex.excluded_tasks_count}",
        "",
    ]
    if ex.by_reason:
        lines.extend(
            [
                "| Exclusion Reason | Count |",
                "|---|---|",
            ]
        )
        for reason, count in sorted(ex.by_reason.items()):
            lines.append(f"| `{escape_markdown(reason)}` | {count} |")
        lines.append("")
    return lines


def _render_recommendations(report: ProviderReliabilityReport) -> list[str]:
    """Render recommendation decisions and candidate rankings."""
    lines = [
        "## 2. Recommendations by Task Class and Mode",
        "",
    ]
    if not report.recommendations:
        lines.append("_No recommendations generated._\n")
        return lines

    for rec in report.recommendations:
        title = (
            f"### Task Class: `{escape_markdown(rec.task_class)}` "
            f"({escape_markdown(rec.mutation_mode)})"
        )
        lines.append(title)
        lines.append("")
        if rec.recommended_profile:
            lines.append(f"- **Recommended Profile**: `{escape_markdown(rec.recommended_profile)}`")
        else:
            lines.append("- **Recommended Profile**: _None_")
        if rec.fallback_reason:
            lines.append(f"- **Fallback Reason**: {escape_markdown(rec.fallback_reason)}")
        lines.append("")

        if rec.rankings:
            lines.extend(
                [
                    "| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | "
                    "Med Latency | Recency | Notes |",
                    "|---|---|---|---|---|---|---|---|",
                ]
            )
            for cand in rec.rankings:
                rank_str = str(cand.rank) if cand.rank is not None else "-"
                lat_str = (
                    f"{cand.median_latency_seconds:.1f}s"
                    if cand.median_latency_seconds is not None
                    else "N/A"
                )
                recency_str = (
                    f"{cand.evidence_age_days:.1f}d ago"
                    if cand.evidence_age_days is not None
                    else "N/A"
                )
                notes = (
                    ", ".join(cand.insufficiency_reasons)
                    if cand.insufficiency_reasons
                    else "eligible"
                )
                lines.append(
                    f"| {rank_str} | `{escape_markdown(cand.profile)}` | "
                    f"{'yes' if cand.is_eligible else 'no'} | "
                    f"{cand.sample_size} ({cand.accepted_count}) | "
                    f"{cand.accepted_rate_wilson_lower:.4f} | {lat_str} | "
                    f"{recency_str} | {escape_markdown(notes)} |"
                )
            lines.append("")
    return lines


def _render_evidence_cells(report: ProviderReliabilityReport) -> list[str]:
    """Render granular empirical evidence cells table."""
    lines = [
        "## 3. Evidence Cells Summary",
        "",
    ]
    if not report.evidence_cells:
        lines.append("_No evidence cells observed._\n")
        return lines

    lines.extend(
        [
            "| Task Class | Profile | Mode | N | Accepted Rate (95% CI) | Failures | "
            "Repairs | Interventions | Overrides | Med Latency | Budget Cov |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for c in report.evidence_cells:
        ci = c.accepted_task_rate_ci
        ci_str = f"{c.accepted_task_rate:.2f} [{ci.lower:.2f}, {ci.upper:.2f}]"
        rep_str = f"{c.repairs.total_repaired_tasks} ({c.repairs.repair_rate:.2f})"
        int_str = (
            f"{c.interventions.human_interventions_count} ({c.interventions.intervention_rate:.2f})"
        )
        lat_str = (
            f"{c.terminal_latency.median_seconds:.1f}s"
            if c.terminal_latency.median_seconds is not None
            else "N/A"
        )
        fail_summary = (
            ", ".join(f"{k}:{v}" for k, v in c.typed_failures.items())
            if c.typed_failures
            else "none"
        )
        lines.append(
            f"| `{escape_markdown(c.task_class)}` | `{escape_markdown(c.profile)}` | "
            f"`{escape_markdown(c.mutation_mode)}` | {c.sample_size} | "
            f"{ci_str} | {escape_markdown(fail_summary)} | {rep_str} | {int_str} | "
            f"{c.manual_overrides_count} | {lat_str} | "
            f"{c.budget_field_coverage.budget_coverage_rate:.2f} |"
        )
    lines.append("")
    return lines


def _render_profile_coverage(report: ProviderReliabilityReport) -> list[str]:
    """Render summary table of enabled profile catalog coverage."""
    if not report.profile_coverage:
        return []
    lines = [
        "## Enabled Profile Catalog Coverage",
        "",
        "| Profile | Mode | Evidence Present | Total Samples | Eligible |",
        "|---|---|---|---|---|",
    ]
    for cov in report.profile_coverage:
        lines.append(
            f"| `{escape_markdown(cov.profile)}` | `{escape_markdown(cov.mutation_mode)}` | "
            f"{'yes' if cov.has_evidence else 'no'} | {cov.sample_size} | "
            f"{'yes' if cov.is_eligible else 'no'} |"
        )
    lines.append("")
    return lines


def render_markdown_report(report: ProviderReliabilityReport) -> str:
    """Render deterministic public markdown report."""
    lines: list[str] = []
    lines.extend(_render_header(report))
    lines.extend(_render_profile_coverage(report))
    lines.extend(_render_exclusions(report))
    lines.extend(_render_recommendations(report))
    lines.extend(_render_evidence_cells(report))
    content = "\n".join(lines)
    assert_sanitized_report(report.model_dump(mode="json"))
    return content


__all__ = [
    "ALLOWED_KEYS_BY_LEVEL",
    "assert_sanitized_report",
    "escape_markdown",
    "render_json_report",
    "render_markdown_report",
]
