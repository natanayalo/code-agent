"""Preflight checks and TaskCohortSnapshot capture for M29 mutation evaluation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evaluation.m29_mutation_cohort import M29_MUTATION_PROFILES
from evaluation.m29_mutation_fixture import verify_taskboard_fixture_repo
from evaluation.m29_mutation_isolation import (
    compute_memory_context_hash,
    validate_evaluation_retention_seconds,
    validate_post_overlay_task_spec,
    verify_pair_semantic_equality,
)
from evaluation.m29_mutation_oracles import compute_verifier_source_sha256
from evaluation.m29_mutation_runner_models import TaskCohortSnapshot
from evaluation.m29_mutation_smokes import (
    ExtractorStageSmokeResult,
    run_m29_extractor_stage_smokes,
)
from evaluation.m29_mutation_suite import M29MutationCase, M29MutationSuite
from orchestrator.state import MemoryContext, TaskSpec


def _hash_task_ids(values: Collection[str]) -> str:
    encoded = json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def capture_task_cohort_snapshot(
    *,
    baseline_memory_context: MemoryContext,
    as_of: datetime,
    baseline_ids_by_profile: Mapping[str, Collection[str]] | None = None,
    smoke_result: ExtractorStageSmokeResult | None = None,
    verifier_source_sha256: str | None = None,
) -> TaskCohortSnapshot:
    """Capture validated TaskCohortSnapshot with baseline hashes and window boundaries."""
    mem_hash = compute_memory_context_hash(baseline_memory_context)
    as_of_utc = as_of.astimezone(UTC)
    norm_ids: dict[str, tuple[str, ...]] = {}
    norm_hashes: dict[str, str] = {}
    for profile in M29_MUTATION_PROFILES:
        raw_ids = baseline_ids_by_profile.get(profile, ()) if baseline_ids_by_profile else ()
        sorted_ids = tuple(sorted(set(raw_ids)))
        norm_ids[profile] = sorted_ids
        norm_hashes[profile] = _hash_task_ids(sorted_ids)

    return TaskCohortSnapshot(
        as_of=as_of_utc,
        window_start_at=as_of_utc - timedelta(days=90),
        window_end_at=as_of_utc,
        baseline_memory_context_hash=mem_hash,
        baseline_ids_by_profile=norm_ids,
        frozen_baseline_ids_sha256_by_profile=norm_hashes,
        smoke_result=smoke_result,
        verifier_source_sha256=verifier_source_sha256,
    )


def default_mutation_task_spec(case: M29MutationCase) -> TaskSpec:
    """Build the canonical post-overlay TaskSpec for an M29 mutation case."""
    return TaskSpec(
        task_type="feature",
        goal=case.prompt,
        workspace_mode="clone",
        allowed_actions=list(case.allowed_actions),
        forbidden_actions=["prepare_branch_delivery", "prepare_draft_pr_delivery"],
        verification_commands=[],
        delivery_mode="workspace",
        requires_clarification=False,
        clarification_questions=[],
        requires_permission=False,
        acceptance_criteria=[case.acceptance_oracle],
    )


def default_mutation_constraints() -> dict[str, Any]:
    """Return constraints enforcing M29 post-terminal evaluation separation."""
    return {
        "skip_independent_review": True,
        "post_terminal_quality_evaluation": True,
        "enable_independent_verifier": False,
    }


def _validate_preflight_pair_specs(suite: M29MutationSuite) -> None:
    for pair_idx, (c_a, c_b) in enumerate(suite.pair_cases(), start=1):
        spec_a, spec_b = default_mutation_task_spec(c_a), default_mutation_task_spec(c_b)
        cons_a, cons_b = default_mutation_constraints(), default_mutation_constraints()
        pair_errs = verify_pair_semantic_equality(c_a, c_b, spec_a, spec_b, cons_a, cons_b)
        if pair_errs:
            raise ValueError(f"preflight pair {pair_idx} semantic equality failed: {pair_errs}")

        for case, spec, cons in ((c_a, spec_a, cons_a), (c_b, spec_b, cons_b)):
            spec_errs = validate_post_overlay_task_spec(
                spec,
                cons,
                expected_profile=case.worker_profile,
                actual_profile=case.worker_profile,
                expected_fixture_revision=case.target_repository_revision,
                actual_fixture_revision=case.target_repository_revision,
            )
            if spec_errs:
                raise ValueError(
                    f"preflight TaskSpec validation failed for case {case.case_id}: {spec_errs}"
                )


def run_mutation_preflight_checks(
    *,
    db_url: str,
    workspace_root: Path,
    retention_seconds: int,
    suite: M29MutationSuite,
    baseline_memory_context: MemoryContext,
    smoke_result: ExtractorStageSmokeResult | None = None,
    run_smokes_if_missing: bool = True,
    as_of: datetime | None = None,
    baseline_ids_by_profile: Mapping[str, Collection[str]] | None = None,
    fixture_repo_path: Path | None = None,
    expected_verifier_sha256: str | None = None,
) -> TaskCohortSnapshot:
    """Execute all preflight gates before any live case is dispatched."""
    validate_evaluation_retention_seconds(retention_seconds)

    if workspace_root.resolve() == Path.cwd().resolve():
        raise ValueError("evaluation workspace root cannot be the primary repository root")
    if not db_url.strip():
        raise ValueError("disposable database URL must not be empty")

    verified_smoke = smoke_result
    if verified_smoke is None and run_smokes_if_missing:
        verified_smoke = run_m29_extractor_stage_smokes(db_url)

    if verified_smoke is not None and not verified_smoke.is_valid:
        raise ValueError(f"extractor stage smoke verification failed: {verified_smoke.violations}")

    if len(baseline_memory_context.personal) > 0:
        raise ValueError("personal memory store must be empty for baseline isolation")

    if fixture_repo_path is not None:
        fixture_errs = verify_taskboard_fixture_repo(
            fixture_repo_path, expected_revision=suite.target_repository_revision
        )
        if fixture_errs:
            raise ValueError(
                f"disposable fixture repository validation failed: {'; '.join(fixture_errs)}"
            )

    verifier_sha = expected_verifier_sha256
    if expected_verifier_sha256 is not None:
        actual_sha = compute_verifier_source_sha256()
        if actual_sha != expected_verifier_sha256:
            raise ValueError(
                f"verifier source sha256 mismatch: expected {expected_verifier_sha256}, "
                f"got {actual_sha}"
            )

    _validate_preflight_pair_specs(suite)

    effective_as_of = as_of or datetime.now(UTC) + timedelta(hours=24)
    return capture_task_cohort_snapshot(
        baseline_memory_context=baseline_memory_context,
        as_of=effective_as_of,
        baseline_ids_by_profile=baseline_ids_by_profile,
        smoke_result=verified_smoke,
        verifier_source_sha256=verifier_sha,
    )


__all__ = [
    "capture_task_cohort_snapshot",
    "default_mutation_constraints",
    "default_mutation_task_spec",
    "run_mutation_preflight_checks",
]
