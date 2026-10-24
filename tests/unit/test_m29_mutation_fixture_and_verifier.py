"""Unit tests for disposable taskboard fixture, runner-owned oracles,
and ReadOnlyExternalEvaluator.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evaluation.m29_mutation_fixture import (
    PINNED_TASKBOARD_FIXTURE_REVISION,
    clone_case_workspace_from_fixture,
    create_taskboard_fixture_repo,
    verify_taskboard_fixture_repo,
)
from evaluation.m29_mutation_isolation import create_terminal_snapshot
from evaluation.m29_mutation_oracles import (
    compute_verifier_source_sha256,
    run_baseline_regression_in_process,
    run_topic_acceptance_oracle_in_process,
)
from evaluation.m29_mutation_runner_preflight import run_mutation_preflight_checks
from evaluation.m29_mutation_suite import (
    DEFAULT_M29_NEUTRAL_EVALUATOR,
    NeutralEvaluatorManifest,
    build_m29_mutation_suite,
    load_frozen_mutation_suite,
)
from evaluation.m29_mutation_verifier import (
    BlindedNeutralReviewRequest,
    NeutralReviewOutcome,
    ReadOnlyExternalEvaluator,
    _restore_permissions_for_cleanup,
    _run_subprocess_check,
)
from evaluation.provider_reliability_models import ExecutionIdentity
from orchestrator.state import MemoryContext

REFERENCE_SOLUTIONS: dict[str, str] = {
    "label_normalization": '''"""Reference solution for label normalization."""
import re, unicodedata

def normalize_label(value: str) -> str:
    norm = unicodedata.normalize("NFKC", value).casefold()
    cleaned = "".join(c if c.isalnum() else "-" for c in norm)
    collapsed = re.sub(r"-+", "-", cleaned)
    return collapsed.strip("-")
''',
    "task_ordering": '''"""Reference solution for task ordering."""
from .models import Task

def sort_tasks(tasks: list[Task]) -> list[Task]:
    return sorted(tasks, key=lambda t: (-t.priority, t.created_at, t.id))
''',
    "task_filters": '''"""Reference solution for task filtering."""
from .models import Task

def filter_tasks(
    tasks: list[Task],
    *,
    status: str | None = None,
    owner: str | None = None,
    tag: str | None = None,
) -> list[Task]:
    res = []
    for t in tasks:
        if status is not None and t.status != status:
            continue
        if owner is not None:
            if t.owner is None or t.owner.casefold() != owner.casefold():
                continue
        if tag is not None and tag not in t.tags:
            continue
        res.append(t)
    return res
''',
    "cursor_pagination": '''"""Reference solution for cursor pagination."""
from .models import Page, Task

def paginate_tasks(tasks: list[Task], *, limit: int, after_id: str | None = None) -> Page:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if after_id is None:
        start = 0
    else:
        found = None
        for i, t in enumerate(tasks):
            if t.id == after_id:
                found = i
                break
        if found is None:
            raise ValueError(f"unknown after_id: {after_id}")
        start = found + 1
    items = list(tasks[start : start + limit])
    has_more = (start + limit) < len(tasks)
    cursor = items[-1].id if (has_more and items) else None
    return Page(items=items, next_cursor=cursor)
''',
    "csv_export": '''"""Reference solution for CSV export."""
import csv, io
from .models import Task

def export_tasks_csv(tasks: list[Task]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\\n")
    writer.writerow(["id", "title", "status", "owner", "priority"])
    for t in tasks:
        owner_val = t.owner if t.owner is not None else ""
        writer.writerow([t.id, t.title, t.status, owner_val, t.priority])
    return buf.getvalue()
''',
    "status_transitions": '''"""Reference solution for status transitions."""
ALLOWED = {
    "queued": {"in_progress", "cancelled"},
    "in_progress": {"done", "cancelled"},
    "done": set(),
    "cancelled": set(),
}

def transition_status(current: str, requested: str) -> str:
    if current not in ALLOWED or requested not in ALLOWED:
        raise ValueError(f"Unknown status: {current} or {requested}")
    if current == requested:
        return current
    if requested in ALLOWED[current]:
        return requested
    raise ValueError(f"Disallowed transition from {current} to {requested}")
''',
    "capped_retry_delay": '''"""Reference solution for capped retry delay."""
def retry_delay_seconds(attempt: int, *, base_seconds: int = 1, max_seconds: int = 30) -> int:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if base_seconds < 0 or max_seconds < 0:
        raise ValueError("delays cannot be negative")
    if base_seconds == 0 or max_seconds == 0:
        return 0
    if attempt > 64:
        return max_seconds
    return min(max_seconds, base_seconds * (2 ** (attempt - 1)))
''',
    "event_deduplication": '''"""Reference solution for event deduplication."""
from .models import Event

def deduplicate_events(events: list[Event]) -> list[Event]:
    seen: set[str] = set()
    res = []
    for e in events:
        if e.id not in seen:
            seen.add(e.id)
            res.append(e)
    return res

def count_events_by_type(events: list[Event]) -> dict[str, int]:
    raise NotImplementedError()
''',
    "event_counts": '''"""Reference solution for event counts."""
from .models import Event

def deduplicate_events(events: list[Event]) -> list[Event]:
    raise NotImplementedError()

def count_events_by_type(events: list[Event]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        counts[e.event_type] = counts.get(e.event_type, 0) + 1
    return {k: counts[k] for k in sorted(counts.keys())}
''',
    "task_status_summary": '''"""Reference solution for task status summary."""
from .models import Task

def summarize_tasks(tasks: list[Task]) -> dict[str, int]:
    s = {"total": len(tasks), "queued": 0, "in_progress": 0, "done": 0, "cancelled": 0, "other": 0}
    known = {"queued", "in_progress", "done", "cancelled"}
    for t in tasks:
        if t.status in known:
            s[t.status] += 1
        else:
            s["other"] += 1
    return s
''',
}


class MockReviewerClient:
    """Mock reviewer client for testing ReadOnlyExternalEvaluator review outcomes."""

    def __init__(self, outcome: NeutralReviewOutcome) -> None:
        self.outcome = outcome
        self.received_request: BlindedNeutralReviewRequest | None = None
        self.received_manifest: NeutralEvaluatorManifest | None = None

    def review_blinded_diff(
        self, request: BlindedNeutralReviewRequest, manifest: NeutralEvaluatorManifest
    ) -> NeutralReviewOutcome:
        self.received_request = request
        self.received_manifest = manifest
        return self.outcome


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "taskboard_fixture"
    create_taskboard_fixture_repo(repo_dir)
    return repo_dir


def test_fixture_repo_creation_and_deterministic_sha(fixture_repo: Path) -> None:
    violations = verify_taskboard_fixture_repo(
        fixture_repo, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )
    assert violations == []

    rev_out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fixture_repo, capture_output=True, text=True, check=True
    )
    assert rev_out.stdout.strip() == PINNED_TASKBOARD_FIXTURE_REVISION

    for ref in ("refs/heads/master", "refs/heads/main"):
        out = subprocess.run(
            ["git", "rev-parse", ref], cwd=fixture_repo, capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == PINNED_TASKBOARD_FIXTURE_REVISION


def test_fixture_repo_verification_catches_violations(tmp_path: Path, fixture_repo: Path) -> None:
    assert verify_taskboard_fixture_repo(tmp_path / "nonexistent") != []

    not_git = tmp_path / "not_git"
    not_git.mkdir()
    assert verify_taskboard_fixture_repo(not_git) != []

    assert verify_taskboard_fixture_repo(fixture_repo, expected_revision="1" * 40) != []

    dirty_file = fixture_repo / "dirty.txt"
    dirty_file.write_text("dirty", encoding="utf-8")
    assert verify_taskboard_fixture_repo(fixture_repo) != []
    dirty_file.unlink()

    # Missing or mismatched branch refs trigger violations
    main_ref = fixture_repo / ".git" / "refs" / "heads" / "main"
    main_content = main_ref.read_text(encoding="utf-8")
    main_ref.unlink()
    violations = verify_taskboard_fixture_repo(fixture_repo)
    assert any("failed to resolve branch ref refs/heads/main" in v for v in violations)

    main_ref.write_text("0" * 40 + "\n", encoding="utf-8")
    violations = verify_taskboard_fixture_repo(fixture_repo)
    assert any("fixture branch refs/heads/main mismatch" in v for v in violations)
    main_ref.write_text(main_content, encoding="utf-8")
    assert verify_taskboard_fixture_repo(fixture_repo) == []


def test_clone_case_workspace_from_fixture(tmp_path: Path, fixture_repo: Path) -> None:
    ws_dir = tmp_path / "case_ws"
    rev = clone_case_workspace_from_fixture(
        fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )
    assert rev == PINNED_TASKBOARD_FIXTURE_REVISION
    assert (ws_dir / "taskboard" / "models.py").is_file()

    with pytest.raises(ValueError, match="failed to checkout expected revision|revision mismatch"):
        bad_ws = tmp_path / "bad_ws"
        clone_case_workspace_from_fixture(fixture_repo, bad_ws, expected_revision="0" * 40)


def test_baseline_regression_passes_and_all_oracles_fail_on_baseline(
    tmp_path: Path, fixture_repo: Path
) -> None:
    ws_dir = tmp_path / "ws_baseline"
    clone_case_workspace_from_fixture(
        fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )

    run_baseline_regression_in_process(ws_dir)

    suite = build_m29_mutation_suite()
    unique_topics = {c.topic for c in suite.cases}
    assert len(unique_topics) == 10

    for topic in unique_topics:
        with pytest.raises((NotImplementedError, AssertionError)):
            run_topic_acceptance_oracle_in_process(topic, ws_dir)


def test_all_ten_reference_solutions_pass_oracles(tmp_path: Path, fixture_repo: Path) -> None:
    suite = build_m29_mutation_suite()
    unique_cases = list({c.topic: c for c in suite.cases}.values())
    assert len(unique_cases) == 10
    for case in unique_cases:
        ws_dir = tmp_path / f"ws_{case.topic}"
        clone_case_workspace_from_fixture(
            fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
        )
        sol_code = REFERENCE_SOLUTIONS[case.topic]
        (ws_dir / case.allowed_source_path).write_text(sol_code, encoding="utf-8")

        run_baseline_regression_in_process(ws_dir)
        run_topic_acceptance_oracle_in_process(case.topic, ws_dir)


def test_oracle_edge_cases_and_rejections(tmp_path: Path, fixture_repo: Path) -> None:
    ws_dir = tmp_path / "ws_edge"
    clone_case_workspace_from_fixture(
        fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )

    bad_label = '''"""Bad label."""
def normalize_label(val: str) -> str:
    return val.lower()
'''
    (ws_dir / "taskboard" / "labels.py").write_text(bad_label, encoding="utf-8")
    with pytest.raises(AssertionError):
        run_topic_acceptance_oracle_in_process("label_normalization", ws_dir)

    bad_order = '''"""Bad order."""
from .models import Task
def sort_tasks(tasks: list[Task]) -> list[Task]:
    return list(reversed(tasks))
'''
    (ws_dir / "taskboard" / "ordering.py").write_text(bad_order, encoding="utf-8")
    with pytest.raises(AssertionError):
        run_topic_acceptance_oracle_in_process("task_ordering", ws_dir)

    bad_csv = '''"""Bad csv."""
from .models import Task
def export_tasks_csv(tasks: list[Task]) -> str:
    return "id,title,status,owner,priority\\r\\n"
'''
    (ws_dir / "taskboard" / "export.py").write_text(bad_csv, encoding="utf-8")
    with pytest.raises(AssertionError, match="carriage return"):
        run_topic_acceptance_oracle_in_process("csv_export", ws_dir)


def test_blinded_neutral_review_request_has_no_provider_or_case_id() -> None:
    req = BlindedNeutralReviewRequest(
        case_topic="label_normalization",
        prompt="Implement normalize_label",
        acceptance_oracle="Acceptance oracle text",
        allowed_source_path="taskboard/labels.py",
        diff_text="+code",
        changed_files=("taskboard/labels.py",),
    )
    assert not hasattr(req, "case_id")
    assert not hasattr(req, "provider")
    assert not hasattr(req, "pair_order")
    assert not hasattr(req, "worker_profile")


def test_read_only_external_evaluator_happy_path(tmp_path: Path, fixture_repo: Path) -> None:
    ws_dir = tmp_path / "ws_eval_happy"
    clone_case_workspace_from_fixture(
        fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )
    case = load_frozen_mutation_suite().cases[0]
    sol_code = REFERENCE_SOLUTIONS[case.topic]
    (ws_dir / case.allowed_source_path).write_text(sol_code, encoding="utf-8")

    terminal_snap = create_terminal_snapshot(
        task_status="completed",
        terminal_event_type="workflow_completed",
        terminal_timestamp=datetime.now(UTC),
        changed_files=[case.allowed_source_path],
    )

    manifest = case.neutral_evaluator or DEFAULT_M29_NEUTRAL_EVALUATOR
    reviewer = MockReviewerClient(
        NeutralReviewOutcome(
            passed=True,
            reason=None,
            evaluator_identity=manifest.identity,
            evaluator_identity_status="verified",
        )
    )
    evaluator = ReadOnlyExternalEvaluator(reviewer_client=reviewer)
    res = evaluator.evaluate_case(case, ws_dir, terminal_snap)

    assert res.quality_status == "pass"
    assert res.acceptance_oracle_passed is True
    assert res.regression_suite_passed is True
    assert res.neutral_review_passed is True
    assert res.evaluator_identity_matches is True
    assert len(res.commands_run) == 2
    assert res.exit_codes == (0, 0)

    # Verify that omitting reviewer_client returns not_assessable
    eval_no_client = ReadOnlyExternalEvaluator(reviewer_client=None)
    res_no_client = eval_no_client.evaluate_case(case, ws_dir, terminal_snap)
    assert res_no_client.quality_status == "not_assessable"
    assert res_no_client.reason == "neutral_reviewer_client_unavailable"
    assert res_no_client.neutral_review_passed is False
    assert res_no_client.evaluator_identity_status == "unavailable"


def test_read_only_external_evaluator_unimplemented_baseline_fails(
    tmp_path: Path, fixture_repo: Path
) -> None:
    ws_dir = tmp_path / "ws_eval_stub"
    clone_case_workspace_from_fixture(
        fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )
    case = load_frozen_mutation_suite().cases[0]
    terminal_snap = create_terminal_snapshot(
        task_status="completed",
        terminal_event_type="workflow_completed",
        terminal_timestamp=datetime.now(UTC),
        changed_files=[case.allowed_source_path],
    )

    manifest = case.neutral_evaluator or DEFAULT_M29_NEUTRAL_EVALUATOR
    reviewer = MockReviewerClient(
        NeutralReviewOutcome(
            passed=True,
            reason=None,
            evaluator_identity=manifest.identity,
            evaluator_identity_status="verified",
        )
    )
    evaluator = ReadOnlyExternalEvaluator(reviewer_client=reviewer)
    res = evaluator.evaluate_case(case, ws_dir, terminal_snap)

    assert res.quality_status == "fail"
    assert res.acceptance_oracle_passed is False
    assert res.regression_suite_passed is True
    assert res.reason == "acceptance_oracle_failed"


def test_read_only_external_evaluator_with_reviewer_client(
    tmp_path: Path, fixture_repo: Path
) -> None:
    ws_dir = tmp_path / "ws_eval_client"
    clone_case_workspace_from_fixture(
        fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )
    case = load_frozen_mutation_suite().cases[0]
    sol_code = REFERENCE_SOLUTIONS[case.topic]
    (ws_dir / case.allowed_source_path).write_text(sol_code, encoding="utf-8")
    terminal_snap = create_terminal_snapshot(
        task_status="completed",
        terminal_event_type="workflow_completed",
        terminal_timestamp=datetime.now(UTC),
        changed_files=[case.allowed_source_path],
    )

    unavail_outcome = NeutralReviewOutcome(
        passed=False,
        evaluator_identity=None,
        evaluator_identity_status="unavailable",
        reason="service_503",
    )
    mock_unavail = MockReviewerClient(unavail_outcome)
    res_unavail = ReadOnlyExternalEvaluator(reviewer_client=mock_unavail).evaluate_case(
        case, ws_dir, terminal_snap
    )
    assert res_unavail.quality_status == "not_assessable"
    assert res_unavail.reason == "service_503"

    mismatch_outcome = NeutralReviewOutcome(
        passed=True,
        evaluator_identity=ExecutionIdentity(provider="rogue", model="m", reasoning_effort="low"),
        evaluator_identity_status="mismatched",
    )
    mock_mismatch = MockReviewerClient(mismatch_outcome)
    res_mismatch = ReadOnlyExternalEvaluator(reviewer_client=mock_mismatch).evaluate_case(
        case, ws_dir, terminal_snap
    )
    assert res_mismatch.quality_status == "not_assessable"


def test_read_only_external_evaluator_detects_workspace_mutation(
    tmp_path: Path, fixture_repo: Path
) -> None:
    ws_dir = tmp_path / "ws_eval_mutated"
    clone_case_workspace_from_fixture(
        fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )
    case = load_frozen_mutation_suite().cases[0]
    terminal_snap = create_terminal_snapshot(
        task_status="completed",
        terminal_event_type="workflow_completed",
        terminal_timestamp=datetime.now(UTC),
        changed_files=[case.allowed_source_path],
    )

    class MutatingReviewer:
        def review_blinded_diff(
            self, req: BlindedNeutralReviewRequest, manifest: NeutralEvaluatorManifest
        ) -> NeutralReviewOutcome:
            (ws_dir / "rogue.txt").write_text("mutated", encoding="utf-8")
            return NeutralReviewOutcome(passed=True, evaluator_identity=manifest.identity)

    evaluator = ReadOnlyExternalEvaluator(reviewer_client=MutatingReviewer())
    res = evaluator.evaluate_case(case, ws_dir, terminal_snap)
    assert res.quality_status == "not_assessable"
    assert res.reason == "candidate_workspace_mutated_during_external_evaluation"


def test_preflight_checks_with_fixture_and_verifier(tmp_path: Path, fixture_repo: Path) -> None:
    suite = load_frozen_mutation_suite()
    mem = MemoryContext()
    ws_root = tmp_path / "runner_ws_root"
    ws_root.mkdir()
    verifier_sha = compute_verifier_source_sha256()

    snap = run_mutation_preflight_checks(
        db_url="sqlite:///tmp.db",
        workspace_root=ws_root,
        retention_seconds=259200,
        suite=suite,
        baseline_memory_context=mem,
        run_smokes_if_missing=False,
        fixture_repo_path=fixture_repo,
        expected_verifier_sha256=verifier_sha,
    )
    assert snap.verifier_source_sha256 == verifier_sha

    with pytest.raises(ValueError, match="disposable fixture repository validation failed"):
        run_mutation_preflight_checks(
            db_url="sqlite:///tmp.db",
            workspace_root=ws_root,
            retention_seconds=259200,
            suite=suite,
            baseline_memory_context=mem,
            run_smokes_if_missing=False,
            fixture_repo_path=tmp_path / "missing",
            expected_verifier_sha256=verifier_sha,
        )

    with pytest.raises(ValueError, match="verifier source sha256 mismatch"):
        run_mutation_preflight_checks(
            db_url="sqlite:///tmp.db",
            workspace_root=ws_root,
            retention_seconds=259200,
            suite=suite,
            baseline_memory_context=mem,
            run_smokes_if_missing=False,
            fixture_repo_path=fixture_repo,
            expected_verifier_sha256="0" * 64,
        )


def test_subprocess_check_timeout_and_error() -> None:
    _, code, out = _run_subprocess_check(
        [sys.executable, "-c", "import time; time.sleep(2)"], timeout=1
    )
    assert code == 124
    assert "subprocess timed out" in out

    _, code2, out2 = _run_subprocess_check(["/nonexistent_command_xyz_123"], timeout=5)
    assert code2 == 1
    assert "subprocess execution failed" in out2


def test_restore_permissions_for_cleanup(tmp_path: Path) -> None:
    _restore_permissions_for_cleanup(tmp_path / "nonexistent")

    test_dir = tmp_path / "ro_dir"
    test_dir.mkdir()
    test_file = test_dir / "ro_file.txt"
    test_file.write_text("content", encoding="utf-8")
    test_file.chmod(0o444)
    test_dir.chmod(0o555)

    _restore_permissions_for_cleanup(test_dir)
    assert test_dir.stat().st_mode & 0o777 == 0o755


def test_oracles_cli_main_invocation(tmp_path: Path, fixture_repo: Path) -> None:
    ws_dir = tmp_path / "ws_cli"
    clone_case_workspace_from_fixture(
        fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )
    (ws_dir / "taskboard" / "labels.py").write_text(
        REFERENCE_SOLUTIONS["label_normalization"], encoding="utf-8"
    )

    from evaluation.m29_mutation_oracles import main

    orig_argv = sys.argv
    try:
        sys.argv = [
            "m29_mutation_oracles.py",
            "--workspace",
            str(ws_dir),
            "--check-baseline",
            "--topic",
            "label_normalization",
        ]
        main()
    finally:
        sys.argv = orig_argv


def test_read_only_evaluator_regression_failure(tmp_path: Path, fixture_repo: Path) -> None:
    suite = build_m29_mutation_suite()
    case = suite.cases[0]
    ws_dir = tmp_path / "ws_reg_fail"
    clone_case_workspace_from_fixture(
        fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )
    (ws_dir / case.allowed_source_path).write_text(
        REFERENCE_SOLUTIONS[case.topic], encoding="utf-8"
    )
    (ws_dir / "taskboard" / "ordering.py").write_text("syntax error !!!", encoding="utf-8")

    manifest = suite.neutral_evaluator
    reviewer = MockReviewerClient(
        NeutralReviewOutcome(
            passed=True,
            reason=None,
            evaluator_identity=manifest.identity,
            evaluator_identity_status="verified",
        )
    )
    evaluator = ReadOnlyExternalEvaluator(reviewer_client=reviewer)
    terminal_snap = create_terminal_snapshot(
        task_status="completed",
        terminal_event_type="workflow_completed",
        terminal_timestamp=datetime.now(UTC),
        changed_files=[case.allowed_source_path],
    )
    res = evaluator.evaluate_case(case, ws_dir, terminal_snap)
    assert res.quality_status == "fail"
    assert res.regression_suite_passed is False
    assert res.reason == "regression_suite_failed"


def test_read_only_evaluator_neutral_review_failure(tmp_path: Path, fixture_repo: Path) -> None:
    suite = build_m29_mutation_suite()
    case = suite.cases[0]
    ws_dir = tmp_path / "ws_rev_fail"
    clone_case_workspace_from_fixture(
        fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )
    (ws_dir / case.allowed_source_path).write_text(
        REFERENCE_SOLUTIONS[case.topic], encoding="utf-8"
    )

    manifest = suite.neutral_evaluator
    reviewer = MockReviewerClient(
        NeutralReviewOutcome(
            passed=False,
            reason=None,
            evaluator_identity=manifest.identity,
            evaluator_identity_status="verified",
        )
    )
    evaluator = ReadOnlyExternalEvaluator(reviewer_client=reviewer)
    terminal_snap = create_terminal_snapshot(
        task_status="completed",
        terminal_event_type="workflow_completed",
        terminal_timestamp=datetime.now(UTC),
        changed_files=[case.allowed_source_path],
    )
    res = evaluator.evaluate_case(case, ws_dir, terminal_snap)
    assert res.quality_status == "fail"
    assert res.neutral_review_passed is False
    assert res.reason == "neutral_review_failed"


def test_read_only_evaluator_immutability_detection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture_repo: Path
) -> None:
    suite = build_m29_mutation_suite()
    case = suite.cases[0]
    ws_dir = tmp_path / "ws_immut"
    clone_case_workspace_from_fixture(
        fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )
    (ws_dir / case.allowed_source_path).write_text(
        REFERENCE_SOLUTIONS[case.topic], encoding="utf-8"
    )

    manifest = suite.neutral_evaluator
    reviewer = MockReviewerClient(
        NeutralReviewOutcome(
            passed=True,
            reason=None,
            evaluator_identity=manifest.identity,
            evaluator_identity_status="verified",
        )
    )
    evaluator = ReadOnlyExternalEvaluator(reviewer_client=reviewer)

    calls = 0

    def fake_tree_hash(p: Path) -> str:
        nonlocal calls
        calls += 1
        return "hash1" if calls == 1 else "hash2"

    monkeypatch.setattr("evaluation.m29_mutation_verifier._compute_tree_sha256", fake_tree_hash)

    terminal_snap = create_terminal_snapshot(
        task_status="completed",
        terminal_event_type="workflow_completed",
        terminal_timestamp=datetime.now(UTC),
        changed_files=[case.allowed_source_path],
    )
    res = evaluator.evaluate_case(case, ws_dir, terminal_snap)
    assert res.quality_status == "not_assessable"
    assert res.reason == "candidate_workspace_mutated_during_external_evaluation"


def test_oracle_extended_contract_rejections(tmp_path: Path, fixture_repo: Path) -> None:
    ws_dir = tmp_path / "ws_contract_rejections"
    clone_case_workspace_from_fixture(
        fixture_repo, ws_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )

    # 1. Baseline file integrity violation
    models_file = ws_dir / "taskboard" / "models.py"
    orig_models = models_file.read_text(encoding="utf-8")
    models_file.write_text(orig_models + "\n# corrupted\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="Protected baseline file modified"):
        run_baseline_regression_in_process(ws_dir)
    models_file.write_text(orig_models, encoding="utf-8")

    # 2. Pair 04: negative limit / middle-page rejection
    bad_page = '''"""Bad pagination."""
from .models import Page, Task
def paginate_tasks(tasks: list[Task], *, limit: int, after_id: str | None = None) -> Page:
    return Page(items=[], next_cursor=None)
'''
    (ws_dir / "taskboard" / "pagination.py").write_text(bad_page, encoding="utf-8")
    with pytest.raises(AssertionError):
        run_topic_acceptance_oracle_in_process("cursor_pagination", ws_dir)

    # 3. Pair 05: file I/O performed
    bad_csv_io = '''"""Bad csv with file I/O."""
from .models import Task
def export_tasks_csv(tasks: list[Task]) -> str:
    with open("/dev/null", "w") as f:
        f.write("")
    return "id,title,status,owner,priority\\n"
'''
    (ws_dir / "taskboard" / "export.py").write_text(bad_csv_io, encoding="utf-8")
    with pytest.raises(AssertionError, match="forbidden file I/O"):
        run_topic_acceptance_oracle_in_process("csv_export", ws_dir)

    # 4. Pair 07: negative max_seconds allowed
    bad_retry = '''"""Bad retry."""
def retry_delay_seconds(attempt: int, *, base_seconds: int = 1, max_seconds: int = 30) -> int:
    return 10
'''
    (ws_dir / "taskboard" / "retry.py").write_text(bad_retry, encoding="utf-8")
    with pytest.raises(AssertionError):
        run_topic_acceptance_oracle_in_process("capped_retry_delay", ws_dir)

    # 5. Pair 09: events list mutation
    bad_events = '''"""Bad events."""
from .models import Event
def deduplicate_events(events: list[Event]) -> list[Event]:
    return events
def count_events_by_type(events: list[Event]) -> dict[str, int]:
    if events:
        events.pop()
    return {"Create": 1, "create": 2, "update": 1}
'''
    (ws_dir / "taskboard" / "events.py").write_text(bad_events, encoding="utf-8")
    with pytest.raises(AssertionError, match="Input events list was mutated"):
        run_topic_acceptance_oracle_in_process("event_counts", ws_dir)

    # 6. Pair 10: tasks list mutation
    bad_summary = '''"""Bad summary."""
from .models import Task
def summarize_tasks(tasks: list[Task]) -> dict[str, int]:
    if tasks:
        tasks.clear()
    return {"total": 5, "queued": 1, "in_progress": 0, "done": 2, "cancelled": 1, "other": 1}
'''
    (ws_dir / "taskboard" / "summary.py").write_text(bad_summary, encoding="utf-8")
    with pytest.raises(AssertionError, match="Input tasks list was mutated"):
        run_topic_acceptance_oracle_in_process("task_status_summary", ws_dir)
