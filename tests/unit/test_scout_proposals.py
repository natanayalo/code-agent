"""Tests for structured Scout proposal contracts."""

from __future__ import annotations

import pytest

from orchestrator.scout_proposals import (
    ScoutProposal,
    ScoutProposalValidationError,
    compute_scout_proposal_fingerprint,
    normalize_scout_worker_result,
    scout_response_schema_for_constraints,
    validate_scout_proposal_payload,
)
from workers import WorkerResult


def _proposal_payload(title: str = "Reduce queue drift") -> dict[str, object]:
    return {
        "title": title,
        "description": "Persist queue lag observations as reviewable Scout proposals.",
        "value": "high",
        "effort": "small",
        "risk": "medium",
        "layer_impact": "orchestrator",
        "validation_path": "Run the execution outcome persistence tests.",
        "hitl_need": "optional",
        "evidence": ["orchestrator/execution_outcome_service.py:236"],
        "implementation_slice": "Add structured Scout proposal persistence.",
    }


def test_scout_response_schema_applies_dynamic_max_items() -> None:
    schema = scout_response_schema_for_constraints({"max_proposals": 2})

    assert schema["required"] == ["proposals"]
    proposals_schema = schema["properties"]["proposals"]
    assert proposals_schema["minItems"] == 1
    assert proposals_schema["maxItems"] == 2
    scout_def = schema["$defs"]["ScoutProposal"]
    assert "implementation_slice" in scout_def["required"]
    assert scout_def["additionalProperties"] is False


def test_validate_scout_proposal_payload_accepts_valid_batch() -> None:
    batch = validate_scout_proposal_payload(
        {"proposals": [_proposal_payload()]},
        max_proposals=3,
    )

    assert batch.proposals[0].title == "Reduce queue drift"
    assert batch.proposals[0].value == "high"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"proposals": []},
        {"proposals": [{"title": "Missing required fields"}]},
    ],
)
def test_validate_scout_proposal_payload_rejects_malformed_batches(
    payload: dict[str, object] | None,
) -> None:
    with pytest.raises(ScoutProposalValidationError):
        validate_scout_proposal_payload(payload, max_proposals=3)


def test_validate_scout_proposal_payload_rejects_too_many_items() -> None:
    with pytest.raises(ScoutProposalValidationError):
        validate_scout_proposal_payload(
            {"proposals": [_proposal_payload("One"), _proposal_payload("Two")]},
            max_proposals=1,
        )


def test_normalize_scout_worker_result_converts_invalid_success_to_failure() -> None:
    result = WorkerResult(
        status="success",
        summary="Scout complete.",
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        json_payload=None,
    )

    normalized = normalize_scout_worker_result(result, constraints={"max_proposals": 3})

    assert normalized.status == "failure"
    assert normalized.failure_kind == "incomplete_delivery"
    assert "did not return a JSON payload" in (normalized.summary or "")


def test_scout_proposal_fingerprint_is_stable_and_phase_sensitive() -> None:
    proposal = ScoutProposal.model_validate(_proposal_payload())
    same = ScoutProposal.model_validate({**_proposal_payload(), "title": "  Reduce queue drift  "})
    changed_phase = compute_scout_proposal_fingerprint(
        proposal,
        task_id="task-1",
        phase="research",
    )

    assert compute_scout_proposal_fingerprint(
        proposal,
        task_id="task-1",
        phase="repo",
    ) == compute_scout_proposal_fingerprint(same, task_id="task-1", phase="repo")
    assert changed_phase != compute_scout_proposal_fingerprint(
        proposal,
        task_id="task-1",
        phase="repo",
    )
