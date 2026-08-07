"""Comprehensive unit tests for orchestrator/review.py helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from orchestrator.review import (
    REPAIR_MAX_PASSES_CONSTRAINT,
    REPAIR_PASSES_USED_CONSTRAINT,
    REPAIR_REQUEST_CONSTRAINT,
    SKIP_INDEPENDENT_REVIEW_CONSTRAINT,
    _apply_independent_review_suppression,
    _build_review_repair_task_text,
    _check_skip_review,
    _cleanup_repair_handoff_constraints,
    _coerce_probability,
    _coerce_string_set,
    _get_reviewer_workers,
    _repair_handoff_update,
    _resolve_repair_handoff_budget,
    _resolve_review_min_severity,
    _resolve_review_timeout_seconds,
    _resolve_style_categories,
    _review_min_confidence_by_severity,
    _session_state_for_review_context,
    _workspace_path_from_result_artifacts,
)
from orchestrator.state import OrchestratorState
from workers import ArtifactReference, WorkerResult
from workers.review import ReviewFinding, ReviewResult

# ---------------------------------------------------------------------------
# _coerce_probability
# ---------------------------------------------------------------------------


def test_coerce_probability_bool():
    assert _coerce_probability(True) is None
    assert _coerce_probability(False) is None


def test_coerce_probability_int_in_range():
    assert _coerce_probability(0) == 0.0
    assert _coerce_probability(1) == 1.0


def test_coerce_probability_float_in_range():
    assert _coerce_probability(0.75) == 0.75


def test_coerce_probability_float_out_of_range():
    assert _coerce_probability(1.5) is None
    assert _coerce_probability(-0.1) is None


def test_coerce_probability_string_valid():
    assert _coerce_probability("0.8") == 0.8


def test_coerce_probability_string_invalid():
    assert _coerce_probability("abc") is None
    assert _coerce_probability("  ") is None
    assert _coerce_probability("") is None


def test_coerce_probability_other():
    assert _coerce_probability(None) is None
    assert _coerce_probability([]) is None


# ---------------------------------------------------------------------------
# _coerce_string_set
# ---------------------------------------------------------------------------


def test_coerce_string_set_from_string():
    result = _coerce_string_set("style, formatting, Naming")
    assert "style" in result
    assert "formatting" in result
    assert "naming" in result


def test_coerce_string_set_from_list():
    result = _coerce_string_set(["Style", "Formatting"])
    assert "style" in result
    assert "formatting" in result


def test_coerce_string_set_list_with_non_strings():
    result = _coerce_string_set(["valid", 42, None])
    assert result == {"valid"}


def test_coerce_string_set_empty_list():
    result = _coerce_string_set([])
    assert result == set()


def test_coerce_string_set_invalid():
    result = _coerce_string_set(42)
    assert result == set()


def test_coerce_string_set_tuple():
    result = _coerce_string_set(("style",))
    assert "style" in result


# ---------------------------------------------------------------------------
# _review_min_confidence_by_severity
# ---------------------------------------------------------------------------


def test_review_min_confidence_by_severity_defaults():
    result = _review_min_confidence_by_severity({})
    assert "low" in result
    assert "medium" in result
    assert "high" in result
    assert "critical" in result


def test_review_min_confidence_by_severity_global():
    result = _review_min_confidence_by_severity({"independent_review_min_confidence": 0.9})
    for v in result.values():
        assert v == 0.9


def test_review_min_confidence_by_severity_per_severity():
    result = _review_min_confidence_by_severity(
        {"independent_review_min_confidence_by_severity": {"high": 0.5, "critical": 0.3}}
    )
    assert result["high"] == 0.5
    assert result["critical"] == 0.3


def test_review_min_confidence_by_severity_invalid_severity_key():
    result = _review_min_confidence_by_severity(
        {"independent_review_min_confidence_by_severity": {"invalid_level": 0.5}}
    )
    # invalid_level is not in SEVERITY_RANK, so ignored
    assert "invalid_level" not in result


def test_review_min_confidence_by_severity_non_mapping():
    result = _review_min_confidence_by_severity(
        {"independent_review_min_confidence_by_severity": "not a mapping"}
    )
    # Should return defaults
    assert "low" in result


# ---------------------------------------------------------------------------
# _resolve_review_min_severity
# ---------------------------------------------------------------------------


def test_resolve_review_min_severity_valid():
    assert _resolve_review_min_severity({"independent_review_min_severity": "high"}) == "high"
    assert (
        _resolve_review_min_severity({"independent_review_min_severity": "CRITICAL"}) == "critical"
    )


def test_resolve_review_min_severity_invalid():
    assert _resolve_review_min_severity({"independent_review_min_severity": "unknown"}) is None
    assert _resolve_review_min_severity({"independent_review_min_severity": 42}) is None
    assert _resolve_review_min_severity({}) is None


# ---------------------------------------------------------------------------
# _resolve_style_categories
# ---------------------------------------------------------------------------


def test_resolve_style_categories_default():
    result = _resolve_style_categories({})
    assert "style" in result
    assert "formatting" in result


def test_resolve_style_categories_include_all():
    result = _resolve_style_categories({"independent_review_include_style_findings": True})
    assert result == set()


def test_resolve_style_categories_configured():
    result = _resolve_style_categories(
        {"independent_review_style_categories": "cosmetic,whitespace"}
    )
    assert "cosmetic" in result
    assert "whitespace" in result


# ---------------------------------------------------------------------------
# _apply_independent_review_suppression
# ---------------------------------------------------------------------------


def _make_finding(severity: str, category: str, confidence: float) -> ReviewFinding:
    return ReviewFinding(
        title="Issue",
        severity=severity,
        category=category,
        confidence=confidence,
        file_path="src/main.py",
        why_it_matters="matters",
    )


def _make_review_result(findings: list[ReviewFinding]) -> ReviewResult:
    return ReviewResult(
        reviewer_kind="independent_reviewer",
        confidence=0.9,
        outcome="findings" if findings else "no_findings",
        summary="review summary",
        findings=findings,
    )


def test_apply_suppression_removes_low_confidence():
    finding = _make_finding("medium", "logic", 0.1)  # below threshold
    review = _make_review_result([finding])
    result = _apply_independent_review_suppression(review, constraints={})
    assert len(result.findings) == 0
    assert len(result.suppressed_findings) == 1


def test_apply_suppression_removes_style():
    finding = _make_finding("high", "style", 0.95)  # style category suppressed
    review = _make_review_result([finding])
    result = _apply_independent_review_suppression(review, constraints={})
    assert len(result.findings) == 0
    assert len(result.suppressed_findings) == 1


def test_apply_suppression_keeps_high_confidence_non_style():
    finding = _make_finding("high", "security", 0.95)
    review = _make_review_result([finding])
    result = _apply_independent_review_suppression(review, constraints={})
    assert len(result.findings) == 1


def test_apply_suppression_min_severity():
    low_finding = _make_finding("low", "logic", 0.95)
    high_finding = _make_finding("high", "security", 0.95)
    review = _make_review_result([low_finding, high_finding])
    result = _apply_independent_review_suppression(
        review,
        constraints={"independent_review_min_severity": "medium"},
    )
    assert len(result.findings) == 1
    assert result.findings[0].severity == "high"


def test_apply_suppression_summary_updated_when_all_suppressed():
    finding = _make_finding("low", "style", 0.5)
    review = _make_review_result([finding])
    result = _apply_independent_review_suppression(review, constraints={})
    assert result.outcome == "no_findings"
    assert "review summary" in result.summary


# ---------------------------------------------------------------------------
# _workspace_path_from_result_artifacts
# ---------------------------------------------------------------------------


def test_workspace_path_from_result_artifacts_no_result():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    assert _workspace_path_from_result_artifacts(state) is None


def test_workspace_path_from_result_artifacts_no_workspace():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(
            status="success",
            artifacts=[
                ArtifactReference(name="log", uri="file:///tmp/log.txt", artifact_type="log")
            ],
        ),
    )
    assert _workspace_path_from_result_artifacts(state) is None


def test_workspace_path_from_result_artifacts_with_workspace():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(
            status="success",
            artifacts=[
                ArtifactReference(
                    name="workspace", uri="file:///tmp/ws1", artifact_type="workspace"
                )
            ],
        ),
    )
    path = _workspace_path_from_result_artifacts(state)
    assert path is not None
    assert str(path) == "/tmp/ws1"


# ---------------------------------------------------------------------------
# _session_state_for_review_context
# ---------------------------------------------------------------------------


def test_session_state_for_review_context_with_update():
    from orchestrator.state import SessionStateUpdate

    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        session_state_update=SessionStateUpdate(active_goal="goal"),
    )
    result = _session_state_for_review_context(state)
    assert result is not None
    assert "active_goal" in result


def test_session_state_for_review_context_no_result():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    result = _session_state_for_review_context(state)
    assert result is None


def test_session_state_for_review_context_with_result():
    state = OrchestratorState(
        task={"task_text": "my task", "repo_url": "url"},
        result=WorkerResult(status="success", files_changed=["a.py", "b.py"]),
    )
    result = _session_state_for_review_context(state)
    assert result is not None
    assert "a.py" in result.get("files_touched", [])


# ---------------------------------------------------------------------------
# _resolve_repair_handoff_budget
# ---------------------------------------------------------------------------


def test_resolve_repair_handoff_budget_defaults():
    max_p, used = _resolve_repair_handoff_budget({})
    assert max_p >= 0
    assert used == 0


def test_resolve_repair_handoff_budget_custom():
    max_p, used = _resolve_repair_handoff_budget(
        {
            REPAIR_MAX_PASSES_CONSTRAINT: 3,
            REPAIR_PASSES_USED_CONSTRAINT: 2,
        }
    )
    assert max_p == 3
    assert used == 2


# ---------------------------------------------------------------------------
# _build_review_repair_task_text
# ---------------------------------------------------------------------------


def test_build_review_repair_task_text():
    finding = _make_finding("high", "security", 0.9)
    finding_with_line = finding.model_copy(update={"line_start": 42})
    text = _build_review_repair_task_text(
        task_text="Implement feature X",
        findings=[finding, finding_with_line],
    )
    assert "Implement feature X" in text
    assert "42" in text  # line number
    assert "security" in text.lower() or "high" in text.lower()


def test_build_review_repair_task_text_no_line():
    finding = _make_finding("medium", "logic", 0.8)
    text = _build_review_repair_task_text(task_text="task", findings=[finding])
    assert "task" in text
    assert "logic" in text.lower() or "medium" in text.lower()


# ---------------------------------------------------------------------------
# _cleanup_repair_handoff_constraints
# ---------------------------------------------------------------------------


def test_cleanup_repair_handoff_constraints():
    constraints = {
        REPAIR_REQUEST_CONSTRAINT: "fix these things",
        SKIP_INDEPENDENT_REVIEW_CONSTRAINT: True,
        "keep_this": "yes",
    }
    cleaned = _cleanup_repair_handoff_constraints(constraints)
    assert REPAIR_REQUEST_CONSTRAINT not in cleaned
    assert SKIP_INDEPENDENT_REVIEW_CONSTRAINT not in cleaned
    assert cleaned["keep_this"] == "yes"


def test_check_skip_review_no_result():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    result = _check_skip_review(state)
    assert result is not None
    assert result.get("current_step") == "review_result"


def test_check_skip_review_read_only():
    state = OrchestratorState(
        task={
            "task_text": "txt",
            "repo_url": "url",
            "constraints": {"read_only": True},
        },
        result=WorkerResult(status="success", files_changed=["a.py"]),
    )
    result = _check_skip_review(state)
    assert result is not None
    assert "read-only" in result["progress_updates"][-1]


def test_check_skip_review_no_files_changed():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="success", files_changed=[]),
    )
    result = _check_skip_review(state)
    assert result is not None
    assert "no files changed" in result["progress_updates"][-1]


def test_check_skip_review_skip_constraint():
    from orchestrator.state import VerificationReport

    state = OrchestratorState(
        task={
            "task_text": "txt",
            "repo_url": "url",
            "constraints": {SKIP_INDEPENDENT_REVIEW_CONSTRAINT: True},
        },
        result=WorkerResult(status="success", files_changed=["a.py"]),
        verification=VerificationReport(status="passed", summary="ok"),
    )
    result = _check_skip_review(state)
    assert result is not None
    assert result.get("current_step") == "review_result"


def test_check_skip_review_failed_verification():
    from orchestrator.state import VerificationReport

    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="success", files_changed=["a.py"]),
        verification=VerificationReport(status="failed", summary="tests failed"),
    )
    result = _check_skip_review(state)
    assert result is not None


def test_check_skip_review_no_skip_condition():
    from orchestrator.state import VerificationReport

    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="success", files_changed=["a.py"]),
        verification=VerificationReport(status="passed", summary="ok"),
    )
    result = _check_skip_review(state)
    assert result is None


# ---------------------------------------------------------------------------
# _get_reviewer_workers
# ---------------------------------------------------------------------------


def test_get_reviewer_workers_empty():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    result = _get_reviewer_workers(state, None)
    assert result == []


def test_get_reviewer_workers_priority():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    factory = {"codex": MagicMock(), "antigravity": MagicMock()}
    workers = _get_reviewer_workers(state, factory)
    names = [name for name, _ in workers]
    assert names[0] == "antigravity"
    assert "codex" in names


def test_get_reviewer_workers_dispatch_fallback():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
    )
    state.dispatch = state.dispatch.model_copy(update={"worker_type": "gemini"})
    gemini = MagicMock()
    workers = _get_reviewer_workers(state, {"gemini": gemini})
    names = [name for name, _ in workers]
    assert "gemini" in names


# ---------------------------------------------------------------------------
# _repair_handoff_update
# ---------------------------------------------------------------------------


def test_repair_handoff_update_no_findings():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    review = _make_review_result([])
    result = _repair_handoff_update(state, review)
    assert result is None


def test_repair_handoff_update_explicit_disabled():
    state = OrchestratorState(
        task={
            "task_text": "txt",
            "repo_url": "url",
            "constraints": {"independent_review_enable_repair_handoff": False},
        }
    )
    finding = _make_finding("high", "security", 0.9)
    review = _make_review_result([finding])
    result = _repair_handoff_update(state, review)
    assert result is None


def test_repair_handoff_update_high_severity_enabled():
    state = OrchestratorState(task={"task_text": "fix this", "repo_url": "url"})
    finding = _make_finding("high", "security", 0.9)
    review = _make_review_result([finding])
    result = _repair_handoff_update(state, review)
    assert result is not None
    assert result["repair_handoff_requested"] is True


def test_repair_handoff_update_max_passes_reached():
    state = OrchestratorState(
        task={
            "task_text": "txt",
            "repo_url": "url",
            "constraints": {
                REPAIR_MAX_PASSES_CONSTRAINT: 1,
                REPAIR_PASSES_USED_CONSTRAINT: 1,
            },
        }
    )
    finding = _make_finding("high", "security", 0.9)
    review = _make_review_result([finding])
    result = _repair_handoff_update(state, review)
    assert result is None


def test_repair_handoff_update_low_severity_not_high_no_explicit():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    finding = _make_finding("low", "style", 0.9)
    review = _make_review_result([finding])
    result = _repair_handoff_update(state, review)
    # Not high severity, no explicit enable → None
    assert result is None


# ---------------------------------------------------------------------------
# _resolve_review_timeout_seconds
# ---------------------------------------------------------------------------


def test_resolve_review_timeout_seconds_default():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    timeout = _resolve_review_timeout_seconds(state)
    assert timeout > 0


def test_resolve_review_timeout_seconds_from_budget():
    state = OrchestratorState(
        task={
            "task_text": "txt",
            "repo_url": "url",
            "budget": {"independent_review_timeout_seconds": 200},
        }
    )
    assert _resolve_review_timeout_seconds(state) == 200


def test_resolve_review_timeout_fallback_orchestrator():
    state = OrchestratorState(
        task={
            "task_text": "txt",
            "repo_url": "url",
            "budget": {"orchestrator_timeout_seconds": 150},
        }
    )
    assert _resolve_review_timeout_seconds(state) == 150
