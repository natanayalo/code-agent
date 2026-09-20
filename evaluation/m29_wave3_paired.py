"""Sanitized Wave 3 manifest contracts and paired descriptive analysis."""

from __future__ import annotations

import hashlib
import json
import random
import statistics
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evaluation.provider_reliability_models import (
    DEFAULT_EXPECTED_EXECUTION_IDENTITIES,
    ProviderReliabilityReport,
    ReliabilityReportPolicy,
)
from evaluation.provider_reliability_report import UNSAFE_VALUE_SUBSTRINGS


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Wave3ManifestCase(_StrictModel):
    """Public, task-id-free case outcome for one frozen suite case."""

    case_id: str = Field(pattern=r"^[a-z0-9-]+$")
    pair_group: str = Field(pattern=r"^[a-z0-9-]+$")
    pair_order: int = Field(ge=1, le=2)
    task_class: Literal["investigation", "feature"]
    worker_profile: str = Field(pattern=r"^[a-z0-9-]+$")
    provider: Literal["codex", "antigravity"]
    terminal_status: Literal["completed", "failed"]
    accepted: bool
    failure_kind: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")
    time_to_terminal_seconds: float | None = Field(default=None, ge=0.0)
    execution_identity_status: Literal["verified", "unknown_legacy", "mixed_execution_identity"]
    identity_matches: bool
    exclusion_reason: str | None = None


class Wave3Manifest(_StrictModel):
    """Sanitized case-level manifest bound to the frozen build and reports."""

    schema_version: Literal[1] = 1
    suite_name: Literal["m29-live-provider-evidence-wave3"]
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    target_repository_revision: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    as_of: datetime
    advisory_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operational_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    robustness_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: list[Wave3ManifestCase]

    @model_validator(mode="after")
    def validate_cases(self) -> Wave3Manifest:
        if len(self.cases) != 40:
            raise ValueError(f"Wave 3 manifest must contain 40 cases, got {len(self.cases)}")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Wave 3 manifest contains duplicate case IDs")
        for task_class in ("investigation", "feature"):
            group = [case for case in self.cases if case.task_class == task_class]
            if len(group) != 20:
                raise ValueError(f"Wave 3 manifest must contain 20 {task_class} cases")
            for provider in ("codex", "antigravity"):
                if sum(case.provider == provider for case in group) != 10:
                    raise ValueError(f"{task_class} must contain 10 {provider} cases")
            pairs: dict[str, list[Wave3ManifestCase]] = {}
            for case in group:
                pairs.setdefault(case.pair_group, []).append(case)
            if len(pairs) != 10 or any(
                len(pair) != 2
                or {case.provider for case in pair} != {"codex", "antigravity"}
                or {case.pair_order for case in pair} != {1, 2}
                for pair in pairs.values()
            ):
                raise ValueError(
                    f"{task_class} manifest pairs must contain one provider at orders 1 and 2"
                )
        return self


class PairedLatencySummary(_StrictModel):
    """Distribution of Antigravity-minus-Codex successful latency deltas."""

    sample_size: int = Field(ge=0)
    median_seconds: float | None = Field(default=None)
    mean_seconds: float | None = Field(default=None)
    min_seconds: float | None = Field(default=None)
    max_seconds: float | None = Field(default=None)


class PairedBootstrapSummary(_StrictModel):
    """Paired resampling probabilities, preserving topic-pair membership."""

    iterations: int = Field(ge=1)
    seed: int
    identity_complete_pairs: int = Field(ge=0)
    antigravity_higher_acceptance_probability: float = Field(ge=0.0, le=1.0)
    codex_higher_acceptance_probability: float = Field(ge=0.0, le=1.0)
    tied_acceptance_probability: float = Field(ge=0.0, le=1.0)
    antigravity_faster_success_latency_probability: float | None = Field(
        default=None, ge=0.0, le=1.0
    )


class PairedTaskClassAnalysis(_StrictModel):
    """Paired outcomes and latency comparison for one task class."""

    task_class: Literal["investigation", "feature"]
    pair_count: int = Field(ge=0)
    both_completed_count: int = Field(ge=0)
    codex_only_completed_count: int = Field(ge=0)
    antigravity_only_completed_count: int = Field(ge=0)
    neither_completed_count: int = Field(ge=0)
    identity_complete_pair_count: int = Field(ge=0)
    successful_latency_differences: PairedLatencySummary
    bootstrap: PairedBootstrapSummary


class PairedAnalysisReport(_StrictModel):
    """Public paired supplement for the Wave 3 robustness report."""

    schema_version: Literal[1] = 1
    suite_name: Literal["m29-live-provider-evidence-wave3"]
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: datetime
    analyses: list[PairedTaskClassAnalysis]


def render_paired_markdown(report: PairedAnalysisReport) -> str:
    """Render the paired supplement as a concise sanitized Markdown report."""
    lines = [
        "# M29 Wave 3 Paired Analysis",
        "",
        f"- **Suite**: `{report.suite_name}`",
        f"- **Build SHA**: `{report.build_sha}`",
        f"- **Manifest SHA-256**: `{report.manifest_sha256}`",
        f"- **Observation Reference (`as_of`)**: `{report.as_of.isoformat()}`",
        "",
        (
            "This supplement resamples complete topic pairs; it is descriptive and does not "
            "change routing."
        ),
        "",
        (
            "| Task Class | Pairs | Both Completed | Codex Only | Antigravity Only | Neither | "
            "Identity-Complete Pairs | Successful Delta N | Median AG-Codex (s) |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for analysis in report.analyses:
        latency = analysis.successful_latency_differences
        lines.append(
            f"| `{analysis.task_class}` | {analysis.pair_count} | "
            f"{analysis.both_completed_count} | "
            f"{analysis.codex_only_completed_count} | "
            f"{analysis.antigravity_only_completed_count} | "
            f"{analysis.neither_completed_count} | {analysis.identity_complete_pair_count} | "
            f"{latency.sample_size} | "
            f"{latency.median_seconds if latency.median_seconds is not None else 'N/A'} |"
        )
    lines.extend(["", "## Paired Bootstrap", ""])
    for analysis in report.analyses:
        bootstrap = analysis.bootstrap
        faster_latency = bootstrap.antigravity_faster_success_latency_probability
        lines.extend(
            [
                f"### `{analysis.task_class}`",
                f"- {bootstrap.iterations:,} iterations, seed {bootstrap.seed}; "
                f"{bootstrap.identity_complete_pairs} identity-complete pairs.",
                f"- Antigravity higher acceptance: "
                f"`{bootstrap.antigravity_higher_acceptance_probability:.4f}`; "
                f"Codex higher: `{bootstrap.codex_higher_acceptance_probability:.4f}`; "
                f"tied: `{bootstrap.tied_acceptance_probability:.4f}`.",
                "- Antigravity faster successful latency: "
                f"`{faster_latency if faster_latency is not None else 'N/A'}`.",
                "",
            ]
        )
    assert_sanitized_paired_report(report.model_dump(mode="json"))
    return "\n".join(lines)


def _latency_summary(values: list[float]) -> PairedLatencySummary:
    """Summarize successful paired latency deltas."""
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
    manifest: Wave3Manifest, task_class: str
) -> list[tuple[Wave3ManifestCase, Wave3ManifestCase]]:
    """Return validated Codex/Antigravity pairs for one task class."""
    grouped: dict[str, list[Wave3ManifestCase]] = {}
    for case in manifest.cases:
        if case.task_class == task_class:
            grouped.setdefault(case.pair_group, []).append(case)
    pairs: list[tuple[Wave3ManifestCase, Wave3ManifestCase]] = []
    for pair_group, cases in sorted(grouped.items()):
        if len(cases) != 2 or {case.provider for case in cases} != {"codex", "antigravity"}:
            raise ValueError(f"pair group {pair_group} is not a complete provider pair")
        by_provider = {case.provider: case for case in cases}
        pairs.append((by_provider["codex"], by_provider["antigravity"]))
    return pairs


def _paired_bootstrap(
    pairs: list[tuple[Wave3ManifestCase, Wave3ManifestCase]],
    iterations: int,
    seed: int,
) -> PairedBootstrapSummary:
    """Resample complete identity-valid pairs and compare paired outcomes."""
    valid_pairs = [pair for pair in pairs if pair[0].identity_matches and pair[1].identity_matches]
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
    ag_higher = codex_higher = tied = faster = 0
    latency_trials = 0
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
            if statistics.mean(deltas) < 0:
                faster += 1

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


def analyze_paired_manifest(
    manifest: Wave3Manifest, *, iterations: int = 10_000, seed: int = 29
) -> list[PairedTaskClassAnalysis]:
    """Compute terminal, identity-complete, latency, and paired-bootstrap summaries."""
    analyses: list[PairedTaskClassAnalysis] = []
    for task_class in ("feature", "investigation"):
        pairs = _paired_groups(manifest, task_class)
        both = sum(c.accepted and a.accepted for c, a in pairs)
        codex_only = sum(c.accepted and not a.accepted for c, a in pairs)
        ag_only = sum(a.accepted and not c.accepted for c, a in pairs)
        neither = sum(not c.accepted and not a.accepted for c, a in pairs)
        identity_pairs = sum(c.identity_matches and a.identity_matches for c, a in pairs)
        deltas = [
            a.time_to_terminal_seconds - c.time_to_terminal_seconds
            for c, a in pairs
            if c.accepted
            and a.accepted
            and c.identity_matches
            and a.identity_matches
            and c.time_to_terminal_seconds is not None
            and a.time_to_terminal_seconds is not None
        ]
        analyses.append(
            PairedTaskClassAnalysis(
                task_class=task_class,
                pair_count=len(pairs),
                both_completed_count=both,
                codex_only_completed_count=codex_only,
                antigravity_only_completed_count=ag_only,
                neither_completed_count=neither,
                identity_complete_pair_count=identity_pairs,
                successful_latency_differences=_latency_summary(deltas),
                bootstrap=_paired_bootstrap(pairs, iterations, seed),
            )
        )
    return analyses


def build_paired_report(
    manifest: Wave3Manifest,
    *,
    manifest_sha256: str,
    iterations: int = 10_000,
    seed: int = 29,
) -> PairedAnalysisReport:
    """Build the paired supplement bound to a sanitized manifest."""
    return PairedAnalysisReport(
        suite_name=manifest.suite_name,
        suite_sha256=manifest.suite_sha256,
        build_sha=manifest.build_sha,
        manifest_sha256=manifest_sha256,
        as_of=manifest.as_of,
        analyses=analyze_paired_manifest(manifest, iterations=iterations, seed=seed),
    )


def _validate_manifest_report_policy(
    manifest: Wave3Manifest, report: ProviderReliabilityReport
) -> None:
    """Require the canonical report to use the Wave 3 extraction policy."""
    expected_policy = ReliabilityReportPolicy(
        as_of=manifest.as_of,
        window_start_at=manifest.as_of - timedelta(days=90),
        window_end_at=manifest.as_of,
        min_samples=10,
        evidence_scope="current_execution_cohort",
    )
    policy = report.policy
    if policy.as_of != expected_policy.as_of:
        raise ValueError(
            f"canonical report policy as_of {policy.as_of.isoformat()} does not match "
            f"manifest as_of {manifest.as_of.isoformat()}"
        )
    if policy.evidence_scope != expected_policy.evidence_scope:
        raise ValueError(
            f"canonical report evidence scope {policy.evidence_scope!r} does not match "
            f"{expected_policy.evidence_scope!r}"
        )
    if policy.min_samples != expected_policy.min_samples:
        raise ValueError(
            f"canonical report sample floor {policy.min_samples} does not match "
            f"{expected_policy.min_samples}"
        )
    if policy.lookback_days != expected_policy.lookback_days:
        raise ValueError(
            f"canonical report lookback {policy.lookback_days} does not match "
            f"{expected_policy.lookback_days}"
        )
    if policy.window_start_at != expected_policy.window_start_at:
        raise ValueError("canonical report policy window start does not match manifest")
    if policy.window_end_at != expected_policy.window_end_at:
        raise ValueError("canonical report policy window end does not match manifest")
    if policy.expected_execution_identities != DEFAULT_EXPECTED_EXECUTION_IDENTITIES:
        raise ValueError("canonical report execution identities do not match the Wave 3 policy")


def assert_manifest_matches_report(
    manifest: Wave3Manifest, report: ProviderReliabilityReport
) -> None:
    """Reconcile Wave 3 inclusion and acceptance with canonical report cells."""
    _validate_manifest_report_policy(manifest, report)
    cells = {
        (cell.task_class, cell.profile, cell.mutation_mode): cell for cell in report.evidence_cells
    }
    for task_class in ("investigation", "feature"):
        for provider in ("codex", "antigravity"):
            profile = f"{provider}-native-executor-read-only"
            group = [
                case
                for case in manifest.cases
                if case.task_class == task_class and case.provider == provider
            ]
            cell = cells.get((task_class, profile, "read_only"))
            if cell is None:
                raise ValueError(f"canonical report is missing Wave 3 cell {task_class}/{profile}")
            included_cases = [case for case in group if case.exclusion_reason is None]
            if any(
                case.execution_identity_status != "verified" or not case.identity_matches
                for case in included_cases
            ):
                raise ValueError(
                    f"manifest includes a case without a verified matching identity in "
                    f"{task_class}/{profile}"
                )
            included = len(included_cases)
            accepted = sum(case.accepted for case in included_cases)
            if cell.sample_size != included or cell.accepted_count != accepted:
                raise ValueError(
                    f"manifest/report mismatch for {task_class}/{profile}: "
                    f"manifest included={included}, accepted={accepted}; "
                    f"report sample={cell.sample_size}, accepted={cell.accepted_count}"
                )


def assert_sanitized_manifest(payload: dict | str) -> None:
    """Reject private identifiers, paths, URLs, and undeclared manifest keys."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    Wave3Manifest.model_validate(data)
    allowed = {
        "schema_version",
        "suite_name",
        "suite_sha256",
        "build_sha",
        "target_repository_revision",
        "as_of",
        "advisory_report_sha256",
        "operational_report_sha256",
        "robustness_report_sha256",
        "cases",
    }
    case_allowed = set(Wave3ManifestCase.model_fields)
    if set(data) != allowed:
        raise ValueError(f"unexpected manifest keys: {sorted(set(data) - allowed)}")
    for case in data["cases"]:
        if set(case) != case_allowed:
            raise ValueError(f"unexpected case keys: {sorted(set(case) - case_allowed)}")
    _scan_public_values(data)


def assert_sanitized_paired_report(payload: dict | str) -> None:
    """Reject private fields from the committed paired analysis supplement."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    PairedAnalysisReport.model_validate(data)
    allowed = {
        "schema_version",
        "suite_name",
        "suite_sha256",
        "build_sha",
        "manifest_sha256",
        "as_of",
        "analyses",
    }
    if set(data) != allowed:
        raise ValueError(f"unexpected paired report keys: {sorted(set(data) - allowed)}")
    _scan_public_values(data)


def _scan_public_values(value: object, path: str = "root") -> None:
    """Scan nested manifest values for private-content markers."""
    if isinstance(value, str):
        if any(fragment in value for fragment in UNSAFE_VALUE_SUBSTRINGS):
            raise ValueError(f"unsafe value detected at {path}")
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in {"task_id", "task_text", "repo_url", "branch", "secrets", "summary", "logs"}:
                raise ValueError(f"private key detected in manifest: {key}")
            _scan_public_values(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_public_values(child, f"{path}[{index}]")


def manifest_sha256(manifest: Wave3Manifest) -> str:
    """Hash the canonical sanitized manifest JSON."""
    payload = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "PairedAnalysisReport",
    "PairedTaskClassAnalysis",
    "Wave3Manifest",
    "Wave3ManifestCase",
    "analyze_paired_manifest",
    "assert_manifest_matches_report",
    "assert_sanitized_manifest",
    "assert_sanitized_paired_report",
    "build_paired_report",
    "manifest_sha256",
    "render_paired_markdown",
]
