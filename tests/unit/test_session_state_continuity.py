"""Regression coverage for M28 typed compact-session continuity."""

from __future__ import annotations

from orchestrator.graph import summarize_result
from orchestrator.state import OrchestratorState

_OUTCOME_STATE = {
    "task": {"task_text": "Fix the persisted task state"},
    "task_spec": {
        "goal": "Fix the persisted task state",
        "task_type": "bugfix",
        "delivery_mode": "draft_pr",
        "risk_level": "high",
        "requires_permission": True,
    },
    "route": {"chosen_worker": "codex", "chosen_profile": "codex-native"},
    "dispatch": {"worker_type": "antigravity", "worker_profile": "agy-native"},
    "approval": {"required": True, "status": "approved"},
    "verification": {"status": "failed", "failure_kind": "test_regression"},
    "review": {
        "reviewer_kind": "independent_reviewer",
        "summary": "Independent review found a regression.",
        "confidence": 0.9,
        "outcome": "findings",
        "findings": [
            {
                "severity": "high",
                "category": "correctness",
                "confidence": 0.9,
                "file_path": "orchestrator/graph.py",
                "title": "State is not persisted",
                "why_it_matters": "The next task loses its context.",
            }
        ],
    },
    "result": {
        "status": "failure",
        "summary": "Provider summary that must not be parsed.",
        "failure_kind": "test",
        "requested_permission": "workspace_write",
        "review_result": {
            "reviewer_kind": "worker_self_review",
            "summary": "Self review found a regression.",
            "confidence": 0.8,
            "outcome": "findings",
            "findings": [
                {
                    "severity": "medium",
                    "category": "tests",
                    "confidence": 0.8,
                    "file_path": "tests/unit/test_session_state_continuity.py",
                    "title": "Regression coverage is incomplete",
                    "why_it_matters": "The failure could recur.",
                }
            ],
        },
    },
}

_EXPECTED_DECISIONS = {
    "task_type": "bugfix",
    "delivery_mode": "draft_pr",
    "worker_type": "antigravity",
    "worker_profile": "agy-native",
    "approval_status": "approved",
}

_EXPECTED_RISKS = {
    "risk_level": "high",
    "requires_permission": True,
    "worker_status": "failure",
    "worker_failure_kind": "test",
    "requested_permission": "workspace_write",
    "verification_status": "failed",
    "verification_failure_kind": "test_regression",
    "review_findings": [
        {
            "reviewer": "independent_reviewer",
            "severity": "high",
            "category": "correctness",
            "title": "State is not persisted",
            "file_path": "orchestrator/graph.py",
        },
        {
            "reviewer": "worker_self_review",
            "severity": "medium",
            "category": "tests",
            "title": "Regression coverage is incomplete",
            "file_path": "tests/unit/test_session_state_continuity.py",
        },
    ],
}


def test_summarize_result_captures_typed_decisions_and_risks() -> None:
    result = summarize_result(OrchestratorState.model_validate(_OUTCOME_STATE))

    update = result["session_state_update"]
    assert update["decisions_made"] == _EXPECTED_DECISIONS
    assert update["identified_risks"] == _EXPECTED_RISKS


def test_summarize_result_clears_optional_typed_session_outcomes() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "  Keep this current task  "},
            "result": {
                "status": "success",
                "summary": '{"worker_failure_kind": "must not be parsed"}',
            },
        }
    )

    result = summarize_result(state)["session_state_update"]

    assert result["decisions_made"] == {
        "task_type": None,
        "delivery_mode": None,
        "worker_type": None,
        "worker_profile": None,
        "approval_status": "not_required",
    }
    assert result["identified_risks"] == {
        "risk_level": None,
        "requires_permission": None,
        "worker_status": "success",
        "worker_failure_kind": None,
        "requested_permission": None,
        "verification_status": None,
        "verification_failure_kind": None,
        "review_findings": [],
    }


def test_summarize_result_bounds_compact_review_findings() -> None:
    long_title = "x" * 300
    findings = [
        {
            "severity": "low",
            "category": "correctness",
            "confidence": 0.9,
            "file_path": f"module_{index}.py",
            "title": long_title if index == 0 else f"Finding {index}",
            "why_it_matters": "The regression should be visible.",
        }
        for index in range(11)
    ]
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Inspect the review findings"},
            "review": {
                "reviewer_kind": "independent_reviewer",
                "summary": "Review found issues.",
                "confidence": 0.9,
                "outcome": "findings",
                "findings": findings,
            },
            "result": {"status": "success", "summary": "done"},
        }
    )

    review_findings = summarize_result(state)["session_state_update"]["identified_risks"][
        "review_findings"
    ]

    assert len(review_findings) == 10
    assert review_findings[0]["title"] == "x" * 237 + "..."
    assert review_findings[-1]["title"] == "Finding 9"


def test_summarize_result_prioritizes_independent_critical_review_findings() -> None:
    self_review_findings = [
        {
            "severity": "low",
            "category": "correctness",
            "confidence": 0.9,
            "file_path": f"module_{index}.py",
            "title": f"Self finding {index}",
            "why_it_matters": "The regression should be visible.",
        }
        for index in range(10)
    ]
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Inspect the review findings"},
            "review": {
                "reviewer_kind": "independent_reviewer",
                "summary": "Review found a critical issue.",
                "confidence": 0.9,
                "outcome": "findings",
                "findings": [
                    {
                        "severity": "critical",
                        "category": "correctness",
                        "confidence": 0.9,
                        "file_path": "critical.py",
                        "title": "Critical independent finding",
                        "why_it_matters": "The regression must be fixed first.",
                    },
                    {
                        "severity": "low",
                        "category": "correctness",
                        "confidence": 0.9,
                        "file_path": "module_0.py",
                        "title": "Self finding 0",
                        "why_it_matters": "The independent reviewer confirmed it.",
                    },
                ],
            },
            "result": {
                "status": "success",
                "summary": "done",
                "review_result": {
                    "reviewer_kind": "worker_self_review",
                    "summary": "Self review found issues.",
                    "confidence": 0.9,
                    "outcome": "findings",
                    "findings": self_review_findings,
                },
            },
        }
    )

    review_findings = summarize_result(state)["session_state_update"]["identified_risks"][
        "review_findings"
    ]

    assert len(review_findings) == 10
    assert review_findings[0]["title"] == "Critical independent finding"
    assert review_findings[1]["reviewer"] == "independent_reviewer"
    assert review_findings[1]["title"] == "Self finding 0"
