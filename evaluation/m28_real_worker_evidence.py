"""Private bundle storage, gates, and sanitized M28 reporting."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from evaluation.m28_real_worker_models import (
    BundleIdentity,
    PrivatePairCapture,
    PublicEffectivenessReport,
    PublicPairResult,
    RealWorkerBundle,
    RealWorkerSuite,
)

SUITE_PATH = Path(__file__).with_name("m28_real_worker_suite.json")
_FORBIDDEN_KEYS = frozenset(
    {"task_id", "task_text", "repo_url", "value", "summary", "logs", "secrets", "notes", "uri"}
)


def load_suite(path: Path = SUITE_PATH) -> RealWorkerSuite:
    """Load the checked-in matrix and fail closed on drift."""
    return RealWorkerSuite.model_validate_json(path.read_text(encoding="utf-8"))


def suite_digest(suite: RealWorkerSuite) -> str:
    """Return a stable digest of the frozen evaluation contract."""
    raw = json.dumps(suite.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def initialize_bundle(
    bundle_dir: Path, *, suite: RealWorkerSuite, identity: BundleIdentity
) -> RealWorkerBundle:
    """Initialize a private evidence directory without overwriting a collection."""
    bundle_dir.mkdir(parents=True, mode=0o700)
    manifest_path = bundle_dir / "manifest.json"
    if manifest_path.exists():
        raise ValueError(f"bundle already exists: {manifest_path}")
    (bundle_dir / "pairs").mkdir(mode=0o700)
    bundle = RealWorkerBundle(identity=identity, suite_sha256=suite_digest(suite))
    _write_model(manifest_path, bundle, exclusive=True)
    _write_model(bundle_dir / "suite.json", suite, exclusive=True)
    return bundle


def load_bundle(bundle_dir: Path) -> tuple[RealWorkerBundle, RealWorkerSuite]:
    """Load a bundle and confirm its frozen suite remains intact."""
    bundle = RealWorkerBundle.model_validate_json((bundle_dir / "manifest.json").read_text())
    suite = RealWorkerSuite.model_validate_json((bundle_dir / "suite.json").read_text())
    if suite_digest(suite) != bundle.suite_sha256:
        raise ValueError("bundle suite digest mismatch")
    return bundle, suite


def persist_pair(bundle_dir: Path, capture: PrivatePairCapture) -> None:
    """Persist one immutable private pair and advance the manifest atomically."""
    bundle, suite = load_bundle(bundle_dir)
    expected = {case.case_id for case in suite.cases}
    if capture.case_id not in expected:
        raise ValueError(f"unknown suite case: {capture.case_id}")
    if capture.case_id in bundle.completed_case_ids:
        raise ValueError(f"pair already captured: {capture.case_id}")
    pair_path = bundle_dir / "pairs" / f"{capture.case_id}.json"
    _write_model(pair_path, capture, exclusive=True)
    updated = bundle.model_copy(
        update={"completed_case_ids": [*bundle.completed_case_ids, capture.case_id]}
    )
    _write_model(bundle_dir / "manifest.json", updated)


def load_captures(bundle_dir: Path) -> list[PrivatePairCapture]:
    """Load captures in stable order and reject a missing manifest entry."""
    bundle, _ = load_bundle(bundle_dir)
    captures = [
        PrivatePairCapture.model_validate_json(
            (bundle_dir / "pairs" / f"{case_id}.json").read_text(encoding="utf-8")
        )
        for case_id in sorted(bundle.completed_case_ids)
    ]
    if {capture.case_id for capture in captures} != set(bundle.completed_case_ids):
        raise ValueError("bundle capture index mismatch")
    return captures


def evaluate_pair(capture: PrivatePairCapture) -> list[str]:
    """Apply the safety-first scenario contract to private measurements."""
    cold, assisted = capture.cold, capture.assisted
    failures: list[str] = []
    if cold.terminal_status != "completed" or assisted.terminal_status != "completed":
        failures.append("terminal_status")
    if not assisted.session_continuity:
        failures.append("compact_session_continuity")
    if assisted.questions > cold.questions:
        failures.append("questions_regressed")
    if assisted.interventions > cold.interventions:
        failures.append("interventions_regressed")
    marker = f"m28-{capture.scenario}-marker"
    if capture.scenario == "useful_hit":
        if marker not in assisted.command_markers:
            failures.append("useful_memory_not_used")
        if "accepted_low_risk_fresh" not in assisted.accepted_reason_codes:
            failures.append("useful_memory_not_fresh_accepted")
    elif capture.scenario == "irrelevant_rejection":
        if "unrelated" in assisted.memory_keys or marker in assisted.command_markers:
            failures.append("irrelevant_memory_influenced_worker")
    elif capture.scenario == "stale_reverification":
        if "m28-stale" not in assisted.suppressed_keys:
            failures.append("stale_memory_not_suppressed")
        if marker in assisted.command_markers:
            failures.append("stale_memory_influenced_worker")
    elif capture.scenario == "conflict_handling":
        if "m28-conflict-personal" not in assisted.suppressed_keys:
            failures.append("conflicting_personal_memory_not_suppressed")
        if marker not in assisted.command_markers:
            failures.append("project_memory_not_used")
    return sorted(set(failures))


def build_report(bundle_dir: Path) -> PublicEffectivenessReport:
    """Render public allowlisted evidence and an intentionally conservative conclusion."""
    bundle, suite = load_bundle(bundle_dir)
    captures = load_captures(bundle_dir)
    cases: list[PublicPairResult] = []
    scenario_validity: dict[str, set[str]] = {}
    for capture in captures:
        failures = sorted(set(capture.gate_failures) | set(evaluate_pair(capture)))
        valid = not failures
        scenario_validity.setdefault(capture.scenario, set()).add(
            capture.worker_profile if valid else ""
        )
        cases.append(
            PublicPairResult(
                case_id=capture.case_id,
                scenario=capture.scenario,
                worker_profile=capture.worker_profile,
                valid=valid,
                gate_failures=failures,
                cold_questions=capture.cold.questions,
                assisted_questions=capture.assisted.questions,
                cold_interventions=capture.cold.interventions,
                assisted_interventions=capture.assisted.interventions,
                cold_time_to_terminal_seconds=capture.cold.time_to_terminal_seconds,
                assisted_time_to_terminal_seconds=capture.assisted.time_to_terminal_seconds,
            )
        )
    valid_cases = sum(case.valid for case in cases)
    if len(cases) < len(suite.cases):
        conclusion = "incomplete"
    elif valid_cases != len(suite.cases):
        all_failures = {failure for case in cases for failure in case.gate_failures}
        conclusion = (
            "unsafe"
            if any("influenced" in failure or "suppressed" in failure for failure in all_failures)
            else "invalid"
        )
    elif all(
        scenario_validity.get(scenario)
        == set(("codex-native-executor-read-only", "antigravity-native-executor-read-only"))
        for scenario in ("useful_hit", "conflict_handling")
    ):
        conclusion = "effective"
    else:
        conclusion = "inconclusive"
    report = PublicEffectivenessReport(
        suite_name=suite.suite_name,
        build_sha=bundle.identity.build_sha,
        environment=bundle.identity.environment,
        repository_revision=bundle.identity.repository_revision,
        conclusion=conclusion,
        captured_pairs=len(cases),
        valid_pairs=valid_cases,
        pairs=sorted(cases, key=lambda item: item.case_id),
    )
    validate_public_payload(report.model_dump(mode="json"))
    return report


def validate_public_payload(value: Any) -> None:
    """Reject unsafe keys recursively before writing a committed report."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in _FORBIDDEN_KEYS:
                raise ValueError(f"forbidden public field: {key}")
            validate_public_payload(item)
    elif isinstance(value, list):
        for item in value:
            validate_public_payload(item)


def write_public_report(
    report: PublicEffectivenessReport, json_path: Path, markdown_path: Path
) -> None:
    """Write deterministic sanitized JSON and Markdown reports."""
    payload = report.model_dump(mode="json")
    validate_public_payload(payload)
    _write_text(json_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    lines = [
        "# M28 Real-Worker Memory Effectiveness",
        "",
        f"- Conclusion: `{report.conclusion}`",
        f"- Build SHA: `{report.build_sha}`",
        f"- Environment: `{report.environment}`",
        f"- Repository revision: `{report.repository_revision}`",
        f"- Pairs: {report.captured_pairs}/{report.required_pairs}; {report.valid_pairs} valid",
        "",
        "| Pair | Scenario | Profile | Valid |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {case.case_id} | {case.scenario} | {case.worker_profile} | {'yes' if case.valid else 'no'} |"
        for case in report.pairs
    )
    _write_text(markdown_path, "\n".join(lines) + "\n")


def _write_model(path: Path, model: Any, *, exclusive: bool = False) -> None:
    content = model.model_dump_json(indent=2) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
        path.chmod(0o600)
        return
    _write_text(path, content)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(content, encoding="utf-8")
    pending.replace(path)
