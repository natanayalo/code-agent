"""Sanitized Wave 4 investigation manifest and paired analysis."""

from __future__ import annotations

import hashlib
import json
import random
import statistics
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from evaluation.m29_wave3_paired import (
    PairedBootstrapSummary,
    PairedLatencySummary,
    PairedTaskClassAnalysis,
    Wave3ManifestCase,
    _scan_public_values,
    _StrictModel,
)


class Wave4ManifestCase(Wave3ManifestCase):
    """Public, task-id-free outcome for one Wave 4 case."""

    report_failure_kind: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")


class Wave4SupplementalObservation(_StrictModel):
    """Earlier eligible attempt included in the cumulative report, outside paired analysis."""

    case_id: str = Field(pattern=r"^[a-z0-9-]+$")
    task_class: Literal["investigation"]
    pair_group: str = Field(pattern=r"^[a-z0-9-]+$")
    provider: Literal["codex", "antigravity"]
    worker_profile: str = Field(pattern=r"^[a-z0-9-]+$")
    terminal_status: Literal["completed", "failed"]
    accepted: bool
    failure_kind: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")
    report_failure_kind: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")
    time_to_terminal_seconds: float | None = Field(default=None, ge=0.0)
    execution_identity_status: Literal["verified", "unknown_legacy", "mixed_execution_identity"]
    identity_matches: bool
    source: Literal["earlier_wave4_attempt"] = "earlier_wave4_attempt"
    included_in_cumulative_report: Literal[True] = True
    included_in_paired_analysis: Literal[False] = False

    @model_validator(mode="after")
    def validate_included_identity(self) -> Wave4SupplementalObservation:
        if not self.identity_matches or self.execution_identity_status != "verified":
            raise ValueError("supplemental cumulative observations require a verified identity")
        if self.accepted != (self.terminal_status == "completed"):
            raise ValueError("supplemental observation acceptance must match terminal status")
        if not self.accepted and self.report_failure_kind is None:
            raise ValueError("supplemental failed observations require a report failure kind")
        if self.accepted and self.report_failure_kind is not None:
            raise ValueError("supplemental completed observations cannot have a failure kind")
        return self


class Wave4Manifest(_StrictModel):
    """Sanitized case-level manifest bound to cumulative reports."""

    schema_version: Literal[2] = 2
    suite_name: Literal["m29-live-provider-evidence-wave4"]
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    target_repository_revision: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    as_of: datetime
    baseline_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    advisory_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operational_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    robustness_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: list[Wave4ManifestCase]
    supplemental_observations: list[Wave4SupplementalObservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_cases(self) -> Wave4Manifest:
        if len(self.cases) != 20:
            raise ValueError(f"Wave 4 manifest must contain 20 cases, got {len(self.cases)}")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Wave 4 manifest contains duplicate case IDs")
        if {case.task_class for case in self.cases} != {"investigation"}:
            raise ValueError("Wave 4 manifest must contain only investigation cases")
        for provider in ("codex", "antigravity"):
            if sum(case.provider == provider for case in self.cases) != 10:
                raise ValueError(f"Wave 4 must contain 10 {provider} cases")
        pairs: dict[str, list[Wave4ManifestCase]] = {}
        for case in self.cases:
            pairs.setdefault(case.pair_group, []).append(case)
        if len(pairs) != 10:
            raise ValueError(f"Wave 4 must contain 10 pair groups, got {len(pairs)}")
        last_first_provider: str | None = None
        for pair_group, pair in sorted(pairs.items()):
            if (
                len(pair) != 2
                or {case.provider for case in pair} != {"codex", "antigravity"}
                or {case.pair_order for case in pair} != {1, 2}
            ):
                raise ValueError(f"pair group {pair_group} is not a complete provider pair")
            ordered = sorted(pair, key=lambda case: case.pair_order)
            if last_first_provider == ordered[0].provider:
                raise ValueError(f"pair group {pair_group} does not alternate provider order")
            last_first_provider = ordered[0].provider
        cases_by_id = {case.case_id: case for case in self.cases}
        for observation in self.supplemental_observations:
            case = cases_by_id.get(observation.case_id)
            if case is None:
                raise ValueError("supplemental observation must reference a frozen Wave 4 case")
            if (
                observation.provider != case.provider
                or observation.worker_profile != case.worker_profile
                or observation.pair_group != case.pair_group
            ):
                raise ValueError(
                    "supplemental observation provider/profile/pair must match its Wave 4 case"
                )
        return self


class Wave4PairedAnalysisReport(_StrictModel):
    """Public paired supplement for the Wave 4 investigation wave."""

    schema_version: Literal[1] = 1
    suite_name: Literal["m29-live-provider-evidence-wave4"]
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: datetime
    analyses: list[PairedTaskClassAnalysis]


def _latency_summary(values: list[float]) -> PairedLatencySummary:
    """Summarize paired successful latency deltas."""
    if not values:
        return PairedLatencySummary(sample_size=0)
    return PairedLatencySummary(
        sample_size=len(values),
        median_seconds=round(statistics.median(values), 2),
        mean_seconds=round(statistics.mean(values), 2),
        min_seconds=round(min(values), 2),
        max_seconds=round(max(values), 2),
    )


def _paired_groups(
    manifest: Wave4Manifest,
) -> list[tuple[Wave4ManifestCase, Wave4ManifestCase]]:
    """Return deterministic Codex/Antigravity pairs."""
    grouped: dict[str, list[Wave4ManifestCase]] = {}
    for case in manifest.cases:
        grouped.setdefault(case.pair_group, []).append(case)
    pairs = []
    for pair_group, cases in sorted(grouped.items()):
        by_provider = {case.provider: case for case in cases}
        if set(by_provider) != {"codex", "antigravity"}:
            raise ValueError(f"pair group {pair_group} is incomplete")
        pairs.append((by_provider["codex"], by_provider["antigravity"]))
    return pairs


def _paired_bootstrap(
    pairs: list[tuple[Wave4ManifestCase, Wave4ManifestCase]],
    iterations: int,
    seed: int,
) -> PairedBootstrapSummary:
    """Resample complete identity-valid pairs."""
    valid_pairs = [pair for pair in pairs if _pair_is_eligible(pair)]
    if not valid_pairs:
        return PairedBootstrapSummary(
            iterations=iterations,
            seed=seed,
            identity_complete_pairs=0,
            antigravity_higher_acceptance_probability=0.0,
            codex_higher_acceptance_probability=0.0,
            tied_acceptance_probability=1.0,
        )

    rng = random.Random(seed)
    ag_higher = codex_higher = tied = faster = latency_trials = 0
    for _ in range(iterations):
        sampled = rng.choices(valid_pairs, k=len(valid_pairs))
        codex_rate = sum(c.accepted for c, _ in sampled) / len(sampled)
        ag_rate = sum(a.accepted for _, a in sampled) / len(sampled)
        if ag_rate > codex_rate:
            ag_higher += 1
        elif codex_rate > ag_rate:
            codex_higher += 1
        else:
            tied += 1
        deltas = [
            a.time_to_terminal_seconds - c.time_to_terminal_seconds
            for c, a in sampled
            if c.accepted
            and a.accepted
            and c.time_to_terminal_seconds is not None
            and a.time_to_terminal_seconds is not None
        ]
        if deltas:
            latency_trials += 1
            faster += statistics.mean(deltas) < 0
    return PairedBootstrapSummary(
        iterations=iterations,
        seed=seed,
        identity_complete_pairs=len(valid_pairs),
        antigravity_higher_acceptance_probability=round(ag_higher / iterations, 4),
        codex_higher_acceptance_probability=round(codex_higher / iterations, 4),
        tied_acceptance_probability=round(tied / iterations, 4),
        antigravity_faster_success_latency_probability=(
            round(faster / latency_trials, 4) if latency_trials else None
        ),
    )


def _case_is_eligible(case: Wave4ManifestCase) -> bool:
    """Return whether a case belongs to the canonical comparison cohort."""
    return case.identity_matches and case.exclusion_reason is None


def _pair_is_eligible(
    pair: tuple[Wave4ManifestCase, Wave4ManifestCase],
) -> bool:
    """Require both members of a pair to be in the canonical comparison cohort."""
    return all(_case_is_eligible(case) for case in pair)


def analyze_wave4_manifest(
    manifest: Wave4Manifest, *, iterations: int = 10_000, seed: int = 29
) -> PairedTaskClassAnalysis:
    """Compute the Wave 4 investigation paired summary."""
    pairs = _paired_groups(manifest)
    both = sum(c.accepted and a.accepted for c, a in pairs)
    codex_only = sum(c.accepted and not a.accepted for c, a in pairs)
    ag_only = sum(a.accepted and not c.accepted for c, a in pairs)
    neither = sum(not c.accepted and not a.accepted for c, a in pairs)
    identity_pairs = sum(_pair_is_eligible((c, a)) for c, a in pairs)
    deltas = [
        a.time_to_terminal_seconds - c.time_to_terminal_seconds
        for c, a in pairs
        if c.accepted
        and a.accepted
        and _case_is_eligible(c)
        and _case_is_eligible(a)
        and c.time_to_terminal_seconds is not None
        and a.time_to_terminal_seconds is not None
    ]
    return PairedTaskClassAnalysis(
        task_class="investigation",
        pair_count=len(pairs),
        both_completed_count=both,
        codex_only_completed_count=codex_only,
        antigravity_only_completed_count=ag_only,
        neither_completed_count=neither,
        identity_complete_pair_count=identity_pairs,
        successful_latency_differences=_latency_summary(deltas),
        bootstrap=_paired_bootstrap(pairs, iterations, seed),
    )


def build_wave4_paired_report(
    manifest: Wave4Manifest, *, manifest_sha256: str, iterations: int = 10_000, seed: int = 29
) -> Wave4PairedAnalysisReport:
    """Build the paired supplement bound to a sanitized manifest."""
    return Wave4PairedAnalysisReport(
        suite_name=manifest.suite_name,
        suite_sha256=manifest.suite_sha256,
        build_sha=manifest.build_sha,
        manifest_sha256=manifest_sha256,
        as_of=manifest.as_of,
        analyses=[analyze_wave4_manifest(manifest, iterations=iterations, seed=seed)],
    )


def render_wave4_markdown(report: Wave4PairedAnalysisReport, manifest: Wave4Manifest) -> str:
    """Render a concise sanitized Markdown supplement."""
    analysis = report.analyses[0]
    latency = analysis.successful_latency_differences
    bootstrap = analysis.bootstrap
    faster_latency = bootstrap.antigravity_faster_success_latency_probability
    eligible_pairs = [pair for pair in _paired_groups(manifest) if _pair_is_eligible(pair)]
    eligible_both = sum(c.accepted and a.accepted for c, a in eligible_pairs)
    eligible_codex_only = sum(c.accepted and not a.accepted for c, a in eligible_pairs)
    eligible_antigravity_only = sum(a.accepted and not c.accepted for c, a in eligible_pairs)
    eligible_neither = sum(not c.accepted and not a.accepted for c, a in eligible_pairs)
    supplemental_count = len(manifest.supplemental_observations)
    supplemental_sentence = (
        f"The manifest also records {supplemental_count} earlier identity-verified "
        f"attempt{'s' if supplemental_count != 1 else ''} included in the cumulative report "
        "but not in this paired analysis."
        if supplemental_count
        else ""
    )
    return "\n".join(
        [
            "# M29 Wave 4 Paired Investigation Analysis",
            "",
            f"- **Suite**: `{report.suite_name}`",
            f"- **Build SHA**: `{report.build_sha}`",
            f"- **Manifest SHA-256**: `{report.manifest_sha256}`",
            f"- **Observation Reference (`as_of`)**: `{report.as_of}`",
            "",
            "This supplement is descriptive and does not change routing.",
            "",
            "| Pairs | Both Completed | Codex Only | Antigravity Only | Neither | "
            "Identity-Complete Pairs | Successful Delta N | Median AG-Codex (s) |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|",
            f"| {analysis.pair_count} | {analysis.both_completed_count} | "
            f"{analysis.codex_only_completed_count} | "
            f"{analysis.antigravity_only_completed_count} | "
            f"{analysis.neither_completed_count} | {analysis.identity_complete_pair_count} | "
            f"{latency.sample_size} | "
            f"{latency.median_seconds if latency.median_seconds is not None else 'N/A'} |",
            "",
            f"Across the {analysis.pair_count} frozen pairs, the table reports all case outcomes. "
            f"Among the {analysis.identity_complete_pair_count} identity-complete pairs, "
            f"{eligible_both} had both providers complete, {eligible_antigravity_only} "
            f"{'was' if eligible_antigravity_only == 1 else 'were'} "
            f"Antigravity-only, {eligible_codex_only} Codex-only, and {eligible_neither} neither.",
            supplemental_sentence,
            "",
            "## Paired Bootstrap",
            "",
            f"- {bootstrap.iterations:,} iterations, seed {bootstrap.seed}; "
            f"{bootstrap.identity_complete_pairs} identity-complete pairs.",
            f"- Antigravity higher acceptance: "
            f"`{bootstrap.antigravity_higher_acceptance_probability:.4f}`; "
            f"Codex higher: `{bootstrap.codex_higher_acceptance_probability:.4f}`; "
            f"tied: `{bootstrap.tied_acceptance_probability:.4f}`.",
            "- Antigravity faster successful latency: "
            f"`{faster_latency if faster_latency is not None else 'N/A'}`.",
        ]
    )


def assert_sanitized_wave4_manifest(payload: dict | str) -> None:
    """Reject private identifiers and undeclared Wave 4 manifest keys."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    Wave4Manifest.model_validate(data)
    allowed = {
        "schema_version",
        "suite_name",
        "suite_sha256",
        "build_sha",
        "target_repository_revision",
        "as_of",
        "baseline_report_sha256",
        "advisory_report_sha256",
        "operational_report_sha256",
        "robustness_report_sha256",
        "cases",
        "supplemental_observations",
    }
    case_allowed = set(Wave4ManifestCase.model_fields)
    supplemental_allowed = set(Wave4SupplementalObservation.model_fields)
    if set(data) != allowed:
        raise ValueError(f"unexpected Wave 4 manifest keys: {sorted(set(data) - allowed)}")
    for case in data["cases"]:
        if set(case) != case_allowed:
            raise ValueError(f"unexpected Wave 4 case keys: {sorted(set(case) - case_allowed)}")
    for observation in data["supplemental_observations"]:
        if set(observation) != supplemental_allowed:
            raise ValueError(
                "unexpected Wave 4 supplemental observation keys: "
                f"{sorted(set(observation) - supplemental_allowed)}"
            )
    _scan_public_values(data)


def assert_sanitized_wave4_paired_report(payload: dict | str) -> None:
    """Reject private fields from the Wave 4 paired supplement."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    Wave4PairedAnalysisReport.model_validate(data)
    _scan_public_values(data)


def wave4_manifest_sha256(manifest: Wave4Manifest) -> str:
    """Hash canonical sanitized Wave 4 manifest JSON."""
    payload = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "Wave4Manifest",
    "Wave4ManifestCase",
    "Wave4SupplementalObservation",
    "Wave4PairedAnalysisReport",
    "analyze_wave4_manifest",
    "assert_sanitized_wave4_manifest",
    "assert_sanitized_wave4_paired_report",
    "build_wave4_paired_report",
    "render_wave4_markdown",
    "wave4_manifest_sha256",
]
