"""Public allowlist validation and markdown rendering for M29 robustness reports."""

from __future__ import annotations

import json
from typing import Any

from evaluation.provider_reliability_models import (
    SAFE_RANK_KEY_PATTERN,
    ProviderReliabilityRobustnessReport,
)
from evaluation.provider_reliability_report import (
    SAFE_CLASS_PATTERN,
    SAFE_IDENTIFIER_PATTERN,
    UNSAFE_VALUE_SUBSTRINGS,
    VALID_EXCLUSION_REASONS,
    VALID_MUTATION_MODES,
    VALID_TASK_CLASSES,
    escape_markdown,
)

ROBUSTNESS_ALLOWED_KEYS_BY_LEVEL: dict[str, frozenset[str]] = {
    "root": frozenset(
        {
            "schema_version",
            "generated_at",
            "status",
            "policy",
            "exclusions",
            "windows",
            "temporal_cohorts",
            "bootstrap_results",
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
            "windows_days",
            "temporal_split_days",
            "bootstrap_iterations",
            "bootstrap_seed",
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
    "exclusions": frozenset(
        {
            "total_tasks_scanned",
            "included_tasks_count",
            "excluded_tasks_count",
            "by_reason",
        }
    ),
    "window": frozenset(
        {
            "window_days",
            "window_start_at",
            "window_end_at",
            "included_tasks_count",
            "recommendations",
        }
    ),
    "temporal_cohort": frozenset(
        {
            "cohort_name",
            "window_start_at",
            "window_end_at",
            "included_tasks_count",
            "recommendations",
        }
    ),
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
    "bootstrap_group": frozenset(
        {
            "task_class",
            "mutation_mode",
            "status",
            "eligible_profiles",
            "candidates",
            "fallback_reason",
        }
    ),
    "bootstrap_candidate": frozenset(
        {
            "profile",
            "sample_size",
            "win_count",
            "win_probability",
            "rank_counts",
            "rank_probabilities",
        }
    ),
}


def _validate_domain_value(key: str, val: Any) -> None:
    """Validate string values against domain boundaries and reject unsafe substrings."""
    if isinstance(val, str):
        for sub in UNSAFE_VALUE_SUBSTRINGS:
            if sub in val:
                raise ValueError(f"Unsafe substring '{sub}' detected in field {key}: {val}")
        if key in (
            "profile",
            "enabled_profiles",
            "eligible_profiles",
        ) and not SAFE_IDENTIFIER_PATTERN.match(val):
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


def _validate_policy_structure(policy_data: dict[str, Any]) -> None:
    """Validate policy dictionary keys, profile identifiers, and expected group tuples."""
    _validate_dict_keys(policy_data, ROBUSTNESS_ALLOWED_KEYS_BY_LEVEL["policy"], "policy")
    for p in policy_data.get("enabled_profiles", []):
        if not isinstance(p, str) or not SAFE_IDENTIFIER_PATTERN.match(p):
            raise ValueError(f"Invalid enabled_profiles identifier format: {p}")
    for prof, ident in policy_data.get("expected_execution_identities", {}).items():
        if not SAFE_IDENTIFIER_PATTERN.match(prof):
            raise ValueError(f"Invalid profile in expected_execution_identities: {prof}")
        if isinstance(ident, dict):
            _validate_dict_keys(
                ident,
                ROBUSTNESS_ALLOWED_KEYS_BY_LEVEL["execution_identity"],
                f"policy.expected_execution_identities[{prof}]",
            )
    for grp in policy_data.get("expected_groups", []):
        if (
            not isinstance(grp, list | tuple)
            or len(grp) != 2
            or not isinstance(grp[0], str)
            or not isinstance(grp[1], str)
            or grp[0] not in VALID_TASK_CLASSES
            or not SAFE_CLASS_PATTERN.match(grp[0])
            or grp[1] not in VALID_MUTATION_MODES
            or not SAFE_CLASS_PATTERN.match(grp[1])
        ):
            raise ValueError(f"Invalid expected_groups entry: {grp}")


def _validate_bootstrap_candidate(cand: dict[str, Any], path: str) -> None:
    """Validate bootstrap candidate fields and nested rank counts/probabilities dictionaries."""
    _validate_dict_keys(
        cand,
        ROBUSTNESS_ALLOWED_KEYS_BY_LEVEL["bootstrap_candidate"],
        path,
    )
    rank_counts = cand.get("rank_counts", {})
    if not isinstance(rank_counts, dict):
        raise ValueError(f"rank_counts must be a dict at {path}")
    for rk, cnt in rank_counts.items():
        if not isinstance(rk, str) or not SAFE_RANK_KEY_PATTERN.match(rk):
            raise ValueError(f"Invalid rank key in rank_counts at {path}: {rk}")
        if not isinstance(cnt, int) or cnt < 0:
            raise ValueError(f"Invalid rank count in rank_counts at {path}: {cnt}")

    rank_probs = cand.get("rank_probabilities", {})
    if not isinstance(rank_probs, dict):
        raise ValueError(f"rank_probabilities must be a dict at {path}")
    for rk, prob in rank_probs.items():
        if not isinstance(rk, str) or not SAFE_RANK_KEY_PATTERN.match(rk):
            raise ValueError(f"Invalid rank key in rank_probabilities at {path}: {rk}")
        if not isinstance(prob, int | float) or not (0.0 <= prob <= 1.0):
            raise ValueError(f"Invalid rank probability in rank_probabilities at {path}: {prob}")


def _validate_recommendations(recs: list[dict[str, Any]], path: str) -> None:
    """Validate list of recommendation structures and their candidate rankings."""
    for idx, rec in enumerate(recs):
        _validate_dict_keys(
            rec, ROBUSTNESS_ALLOWED_KEYS_BY_LEVEL["recommendation"], f"{path}[{idx}]"
        )
        for c_idx, cand in enumerate(rec.get("rankings", [])):
            _validate_dict_keys(
                cand,
                ROBUSTNESS_ALLOWED_KEYS_BY_LEVEL["ranking"],
                f"{path}[{idx}].rankings[{c_idx}]",
            )


def assert_sanitized_robustness_report(payload: dict[str, Any] | str) -> None:
    """Validate that robustness report conforms strictly to public allowlists."""
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid report JSON payload: {exc}") from exc
    else:
        data = payload

    _validate_dict_keys(data, ROBUSTNESS_ALLOWED_KEYS_BY_LEVEL["root"], "root")
    if "policy" in data:
        _validate_policy_structure(data["policy"])
    if "exclusions" in data:
        _validate_dict_keys(
            data["exclusions"], ROBUSTNESS_ALLOWED_KEYS_BY_LEVEL["exclusions"], "exclusions"
        )
        for rk in data["exclusions"].get("by_reason", {}):
            if not SAFE_CLASS_PATTERN.match(rk) or rk not in VALID_EXCLUSION_REASONS:
                raise ValueError(f"Invalid exclusion reason key: {rk}")

    for idx, win in enumerate(data.get("windows", [])):
        _validate_dict_keys(win, ROBUSTNESS_ALLOWED_KEYS_BY_LEVEL["window"], f"windows[{idx}]")
        _validate_recommendations(win.get("recommendations", []), f"windows[{idx}].recommendations")

    for idx, cohort in enumerate(data.get("temporal_cohorts", [])):
        _validate_dict_keys(
            cohort, ROBUSTNESS_ALLOWED_KEYS_BY_LEVEL["temporal_cohort"], f"temporal_cohorts[{idx}]"
        )
        _validate_recommendations(
            cohort.get("recommendations", []), f"temporal_cohorts[{idx}].recommendations"
        )

    for idx, bg in enumerate(data.get("bootstrap_results", [])):
        bg_path = f"bootstrap_results[{idx}]"
        _validate_dict_keys(bg, ROBUSTNESS_ALLOWED_KEYS_BY_LEVEL["bootstrap_group"], bg_path)
        for ep in bg.get("eligible_profiles", []):
            if not isinstance(ep, str) or not SAFE_IDENTIFIER_PATTERN.match(ep):
                raise ValueError(f"Invalid eligible_profiles identifier format at {bg_path}: {ep}")
        for c_idx, cand in enumerate(bg.get("candidates", [])):
            _validate_bootstrap_candidate(cand, f"{bg_path}.candidates[{c_idx}]")

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


def render_json_robustness_report(report: ProviderReliabilityRobustnessReport) -> str:
    """Render the robustness report as a deterministic, sorted, sanitized JSON string."""
    dumped = report.model_dump(mode="json")
    assert_sanitized_robustness_report(dumped)
    return json.dumps(dumped, indent=2, sort_keys=True) + "\n"


def _render_summary_table(report: ProviderReliabilityRobustnessReport) -> list[str]:
    """Render cross-analysis robustness comparison table across windows, cohorts, and bootstrap."""
    lines = [
        "## 1. Executive Robustness Summary",
        "",
        (
            "| Task Class | Mode | 30d Window | 60d Window | 90d Window | "
            "Hist (90d-45d) | Recent (45d-0d) | Bootstrap Winner | Win Prob |"
        ),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    win_map: dict[int, dict[tuple[str, str], str]] = {}
    for w in report.windows:
        win_map[w.window_days] = {
            (r.task_class, r.mutation_mode): r.recommended_profile or "None"
            for r in w.recommendations
        }

    cohort_map: dict[str, dict[tuple[str, str], str]] = {}
    for c in report.temporal_cohorts:
        cohort_map[c.cohort_name] = {
            (r.task_class, r.mutation_mode): r.recommended_profile or "None"
            for r in c.recommendations
        }

    boot_map: dict[tuple[str, str], tuple[str, str]] = {}
    for b in report.bootstrap_results:
        if b.status == "complete" and b.candidates:
            top = b.candidates[0]
            prob_pct = f"{int(round(top.win_probability * 100))}%"
            boot_map[(b.task_class, b.mutation_mode)] = (top.profile, prob_pct)
        else:
            boot_map[(b.task_class, b.mutation_mode)] = ("None", "N/A")

    all_groups = sorted(
        {(r.task_class, r.mutation_mode) for w in report.windows for r in w.recommendations}
    )

    for tc, mode in all_groups:
        w30 = escape_markdown(win_map.get(30, {}).get((tc, mode), "None"))
        w60 = escape_markdown(win_map.get(60, {}).get((tc, mode), "None"))
        w90 = escape_markdown(win_map.get(90, {}).get((tc, mode), "None"))
        hist = escape_markdown(cohort_map.get("historical", {}).get((tc, mode), "None"))
        rec = escape_markdown(cohort_map.get("recent", {}).get((tc, mode), "None"))
        b_win, b_prob = boot_map.get((tc, mode), ("None", "N/A"))
        row = (
            f"| `{escape_markdown(tc)}` | `{escape_markdown(mode)}` | `{w30}` | `{w60}` | "
            f"`{w90}` | `{hist}` | `{rec}` | `{escape_markdown(b_win)}` | {b_prob} |"
        )
        lines.append(row)
    lines.append("")
    return lines


def _render_windows_section(report: ProviderReliabilityRobustnessReport) -> list[str]:
    """Render details of 30, 60, and 90-day lookback window recommendations."""
    lines = ["## 2. Window Variation Analysis", ""]
    for w in sorted(report.windows, key=lambda item: item.window_days):
        lines.append(
            f"### {w.window_days}-Day Lookback Window "
            f"(`{w.window_start_at.isoformat()}` to `{w.window_end_at.isoformat()}`)"
        )
        lines.append(f"- **Included Tasks**: {w.included_tasks_count}")
        lines.append("")
        for r in w.recommendations:
            rec_p = f"`{r.recommended_profile}`" if r.recommended_profile else "_None_"
            lines.append(
                f"- **`{escape_markdown(r.task_class)}` ({escape_markdown(r.mutation_mode)})**: "
                f"Recommended: {rec_p}"
            )
            if r.fallback_reason:
                lines.append(f"  - Fallback: {escape_markdown(r.fallback_reason)}")
        lines.append("")
    return lines


def _render_cohorts_section(report: ProviderReliabilityRobustnessReport) -> list[str]:
    """Render details of non-overlapping temporal split-half cohorts."""
    lines = [
        "## 3. Temporal Split-Half Cohorts",
        "",
        "Evaluates temporal stability across non-overlapping partitions of the 90-day window: "
        "historical `[as_of-90d, as_of-45d)` and recent `[as_of-45d, as_of]`.",
        "",
    ]
    for c in report.temporal_cohorts:
        lines.append(
            f"### Cohort: `{escape_markdown(c.cohort_name)}` "
            f"(`{c.window_start_at.isoformat()}` to `{c.window_end_at.isoformat()}`)"
        )
        lines.append(f"- **Included Tasks**: {c.included_tasks_count}")
        lines.append("")
        for r in c.recommendations:
            rec_p = f"`{r.recommended_profile}`" if r.recommended_profile else "_None_"
            lines.append(
                f"- **`{escape_markdown(r.task_class)}` ({escape_markdown(r.mutation_mode)})**: "
                f"Recommended: {rec_p}"
            )
            if r.fallback_reason:
                lines.append(f"  - Fallback: {escape_markdown(r.fallback_reason)}")
        lines.append("")
    return lines


def _render_bootstrap_section(report: ProviderReliabilityRobustnessReport) -> list[str]:
    """Render bootstrap resampling sensitivity results and rank distributions."""
    iter_str = f"{report.policy.bootstrap_iterations:,}"
    lines = [
        "## 4. Bootstrap Resampling Sensitivity",
        "",
        (
            f"Evaluates {iter_str} deterministic bootstrap iterations "
            f"(seed {report.policy.bootstrap_seed}) resampling whole task observations "
            "independently per profile cell to preserve acceptance and latency correlation. "
            "Advisory-only descriptive results; no automated routing threshold is applied."
        ),
        "",
    ]
    for bg in report.bootstrap_results:
        lines.append(
            f"### Group: `{escape_markdown(bg.task_class)}` ({escape_markdown(bg.mutation_mode)})"
        )
        lines.append(f"- **Status**: `{bg.status}`")
        if bg.status != "complete":
            fb = escape_markdown(bg.fallback_reason or "insufficient_data")
            lines.append(f"- **Fallback Reason**: {fb}")
            lines.append("")
            continue

        lines.extend(
            [
                "",
                (
                    "| Profile | Sample Size | Win Count | Win Prob | "
                    "Rank Counts (1..M) | Rank Probs (1..M) |"
                ),
                "|---|---|---|---|---|---|",
            ]
        )
        for c in bg.candidates:
            rc_str = ", ".join(
                f"R{r}:{cnt}" for r, cnt in sorted(c.rank_counts.items(), key=lambda x: int(x[0]))
            )
            rp_str = ", ".join(
                f"R{r}:{p:.4f}"
                for r, p in sorted(c.rank_probabilities.items(), key=lambda x: int(x[0]))
            )
            lines.append(
                f"| `{escape_markdown(c.profile)}` | {c.sample_size} | {c.win_count} | "
                f"{c.win_probability:.4f} | {escape_markdown(rc_str)} | {escape_markdown(rp_str)} |"
            )
        lines.append("")
    return lines


def _render_exclusions_section(report: ProviderReliabilityRobustnessReport) -> list[str]:
    """Render 90-day observation snapshot accounting and exclusions."""
    ex = report.exclusions
    lines = [
        "## 5. Evidence Accounting & 90-Day Snapshot Exclusions",
        "",
        (
            "Accounting metrics reflect the full 90-day observation snapshot "
            "scanned from the database."
        ),
        "",
        f"- **Total Tasks Scanned**: {ex.total_tasks_scanned}",
        f"- **Included In 90-Day Evidence**: {ex.included_tasks_count}",
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


def render_markdown_robustness_report(report: ProviderReliabilityRobustnessReport) -> str:
    """Render deterministic public markdown robustness report."""
    p = report.policy
    win_str = f"`{p.window_start_at.isoformat()}` to `{p.window_end_at.isoformat()}`"
    iter_str = f"{p.bootstrap_iterations:,}"
    lines = [
        "# M29 Provider Reliability Robustness Report",
        "",
        f"- **Status**: `{report.status}`",
        f"- **Evidence Scope**: `{p.evidence_scope}`",
        f"- **Generated At**: `{report.generated_at.isoformat()}`",
        f"- **Observation Reference (`as_of`)**: `{p.as_of.isoformat()}`",
        f"- **Lookback Window**: {p.lookback_days} days ({win_str})",
        f"- **Minimum Samples Per Cell**: {p.min_samples}",
        f"- **Bootstrap Resampling**: {iter_str} iterations, seed {p.bootstrap_seed}",
        "",
    ]
    lines.extend(_render_summary_table(report))
    lines.extend(_render_windows_section(report))
    lines.extend(_render_cohorts_section(report))
    lines.extend(_render_bootstrap_section(report))
    lines.extend(_render_exclusions_section(report))

    assert_sanitized_robustness_report(report.model_dump(mode="json"))
    return "\n".join(lines)


__all__ = [
    "ROBUSTNESS_ALLOWED_KEYS_BY_LEVEL",
    "assert_sanitized_robustness_report",
    "render_json_robustness_report",
    "render_markdown_robustness_report",
]
