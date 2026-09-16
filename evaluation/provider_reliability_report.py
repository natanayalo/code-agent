"""Formatting, markdown rendering, and public allowlist validation for M29 reports."""

from __future__ import annotations

import json
from typing import Any

from evaluation.provider_reliability_models import (
    ProviderReliabilityReport,
)

FORBIDDEN_KEYS = frozenset(
    {
        "task_id",
        "task_text",
        "repo_url",
        "branch",
        "delivery_branch",
        "callback_url",
        "summary",
        "commands_run",
        "files_changed",
        "raw_history",
        "logs",
        "uri",
        "artifact_uri",
        "secret",
        "secrets",
        "access_token",
        "password",
        "session_id",
        "workspace_id",
        "process_identity",
    }
)


def assert_sanitized_report(payload: dict[str, Any] | str) -> None:
    """Validate that serialized evidence conforms strictly to the public allowlist."""
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid report JSON payload: {exc}") from exc
    else:
        data = payload

    def _scan(item: Any, path: str = "") -> None:
        if isinstance(item, dict):
            for k, v in item.items():
                normalized_key = k.lower().strip()
                if normalized_key in FORBIDDEN_KEYS:
                    raise ValueError(f"Forbidden key detected in public report: {path}.{k}")
                _scan(v, f"{path}.{k}" if path else k)
        elif isinstance(item, list):
            for idx, entry in enumerate(item):
                _scan(entry, f"{path}[{idx}]")

    _scan(data)


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
            lines.append(f"| `{reason}` | {count} |")
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
        title = f"### Task Class: `{rec.task_class}` ({rec.mutation_mode})"
        lines.append(title)
        lines.append("")
        if rec.recommended_profile:
            lines.append(f"- **Recommended Profile**: `{rec.recommended_profile}`")
        else:
            lines.append("- **Recommended Profile**: _None_")
        if rec.fallback_reason:
            lines.append(f"- **Fallback Reason**: {rec.fallback_reason}")
        lines.append("")

        if rec.rankings:
            lines.extend(
                [
                    "| Rank | Profile | Eligible | Wilson 95% Lower | Median Latency | Notes |",
                    "|---|---|---|---|---|---|",
                ]
            )
            for cand in rec.rankings:
                rank_str = str(cand.rank) if cand.rank is not None else "-"
                lat_str = (
                    f"{cand.median_latency_seconds:.1f}s"
                    if cand.median_latency_seconds is not None
                    else "N/A"
                )
                notes = (
                    ", ".join(cand.insufficiency_reasons)
                    if cand.insufficiency_reasons
                    else "eligible"
                )
                lines.append(
                    f"| {rank_str} | `{cand.profile}` | {'yes' if cand.is_eligible else 'no'} | "
                    f"{cand.accepted_rate_wilson_lower:.4f} | {lat_str} | {notes} |"
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
            f"| `{c.task_class}` | `{c.profile}` | `{c.mutation_mode}` | {c.sample_size} | "
            f"{ci_str} | {fail_summary} | {rep_str} | {int_str} | "
            f"{c.manual_overrides_count} | {lat_str} | "
            f"{c.budget_field_coverage.budget_coverage_rate:.2f} |"
        )
    lines.append("")
    return lines


def render_markdown_report(report: ProviderReliabilityReport) -> str:
    """Render deterministic public markdown report."""
    lines: list[str] = []
    lines.extend(_render_header(report))
    lines.extend(_render_exclusions(report))
    lines.extend(_render_recommendations(report))
    lines.extend(_render_evidence_cells(report))
    content = "\n".join(lines)
    assert_sanitized_report(report.model_dump(mode="json"))
    return content
