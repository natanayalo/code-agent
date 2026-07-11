#!/usr/bin/env python3
"""Run deterministic M24.6 control-plane DAG reliability checks."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.graph import _await_decomposed_nodes
from orchestrator.state import OrchestratorState
from workers import Worker, WorkerRequest, WorkerResult


class ScenarioWorker(Worker):
    """A deterministic worker whose outcomes are supplied in call order."""

    def __init__(self, results: list[WorkerResult]) -> None:
        self.results = results
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest, **kwargs: object) -> WorkerResult:
        del kwargs
        self.requests.append(request)
        return self.results.pop(0)


def _state(nodes: list[dict]) -> OrchestratorState:
    return OrchestratorState.model_validate(
        {
            "task": {"task_text": "Evaluate DAG reliability"},
            "task_spec": {"goal": "Evaluate DAG reliability"},
            "route": {"chosen_worker": "codex"},
            "dispatch": {"worker_type": "codex"},
            "decomposed_plan": {"triggered": True, "status": "decomposed", "nodes": nodes},
        }
    )


def _nodes(*, include_verify: bool = False, branching: bool = False) -> list[dict]:
    nodes = [
        {
            "node_id": "inspect",
            "title": "Inspect",
            "task_spec": {"goal": "Inspect"},
            "node_kind": "inspect",
        },
        {
            "node_id": "implement",
            "title": "Implement",
            "depends_on": ["inspect"],
            "task_spec": {"goal": "Implement"},
            "node_kind": "implement",
            "max_attempts": 3,
        },
    ]
    if include_verify or branching:
        nodes += [
            {
                "node_id": "verify",
                "title": "Verify",
                "depends_on": ["inspect"] if branching else ["implement"],
                "task_spec": {"goal": "Verify"},
                "node_kind": "verify",
            },
        ]
    if branching:
        nodes += [
            {
                "node_id": "join",
                "title": "Join",
                "depends_on": ["implement", "verify"],
                "task_spec": {"goal": "Join"},
                "node_kind": "aggregate",
            },
        ]
    return nodes


def _run(name: str, state: OrchestratorState, results: list[WorkerResult]) -> dict[str, object]:
    worker = ScenarioWorker(results)
    result, outcomes, _ = asyncio.run(_await_decomposed_nodes(state, worker))
    return {
        "scenario": name,
        "result_status": result.status,
        "next_action_hint": result.next_action_hint,
        "outcomes": [{"node_id": item.node_id, "status": item.status} for item in outcomes],
        "dispatch_node_ids": [
            request.task_text.split("Current DAG node (")[1].split(")")[0]
            for request in worker.requests
        ],
        "join_dependency_context": next(
            (
                request.memory_context.get("decomposed_task", {})
                for request in worker.requests
                if "Current DAG node (join)" in request.task_text
            ),
            {},
        ),
    }


def _permission_pause_resume() -> dict[str, object]:
    state = _state(_nodes(include_verify=True))
    worker = ScenarioWorker(
        [
            WorkerResult(status="success", summary="inspected"),
            WorkerResult(
                status="failure",
                failure_kind="permission_denied",
                next_action_hint="request_higher_permission",
            ),
            WorkerResult(status="success", summary="implemented"),
            WorkerResult(status="success", summary="verified"),
        ]
    )
    paused_result, paused_outcomes, _ = asyncio.run(_await_decomposed_nodes(state, worker))
    resumed_state = state.model_copy(update={"node_outcomes": paused_outcomes})
    resumed_result, resumed_outcomes, _ = asyncio.run(
        _await_decomposed_nodes(resumed_state, worker)
    )
    outcomes = [{"node_id": item.node_id, "status": item.status} for item in resumed_outcomes]
    return {
        "scenario": "permission_pause_resume",
        "passed": (
            paused_result.next_action_hint == "request_higher_permission"
            and [(item.node_id, item.status) for item in paused_outcomes]
            == [("inspect", "completed"), ("implement", "blocked")]
            and resumed_result.status == "success"
            and outcomes
            == [
                {"node_id": "inspect", "status": "completed"},
                {"node_id": "implement", "status": "completed"},
                {"node_id": "verify", "status": "completed"},
            ]
        ),
        "outcomes": outcomes,
        "dispatch_node_ids": [
            request.task_text.split("Current DAG node (")[1].split(")")[0]
            for request in worker.requests
        ],
    }


def _success() -> WorkerResult:
    return WorkerResult(status="success", summary="ok")


def _reliability_cases() -> list[dict[str, object]]:
    linear_success = _run("linear_success", _state(_nodes()), [_success(), _success()])
    failure_skip = _run(
        "node_failure_downstream_skip",
        _state(_nodes(include_verify=True)),
        [
            _success(),
            WorkerResult(status="failure", failure_kind="worker_failure"),
            WorkerResult(status="failure", failure_kind="worker_failure"),
            WorkerResult(status="failure", failure_kind="worker_failure"),
        ],
    )
    verify_failure = _run(
        "verify_node_failure",
        _state(_nodes(include_verify=True)),
        [
            _success(),
            _success(),
            WorkerResult(status="failure", failure_kind="test"),
        ],
    )
    retry_success = _run(
        "retry_success",
        _state(_nodes()),
        [_success(), WorkerResult(status="failure"), _success()],
    )
    retry_exhaustion = _run(
        "retry_exhaustion",
        _state(_nodes()),
        [
            _success(),
            WorkerResult(status="failure"),
            WorkerResult(status="failure"),
            WorkerResult(status="failure"),
        ],
    )
    branch_join = _run(
        "serial_branch_join",
        _state(_nodes(branching=True)),
        [_success(), _success(), _success(), _success()],
    )
    linear_success["passed"] = linear_success["result_status"] == "success"
    failure_skip["passed"] = failure_skip["outcomes"] == [
        {"node_id": "inspect", "status": "completed"},
        {"node_id": "implement", "status": "failed"},
        {"node_id": "verify", "status": "skipped"},
    ]
    verify_failure["passed"] = verify_failure["outcomes"][-1] == {
        "node_id": "verify",
        "status": "failed",
    }
    retry_success["passed"] = retry_success["result_status"] == "success"
    retry_exhaustion["passed"] = retry_exhaustion["outcomes"][-1]["status"] == "failed"
    branch_join["passed"] = branch_join["dispatch_node_ids"] == [
        "inspect",
        "implement",
        "verify",
        "join",
    ] and set(branch_join["join_dependency_context"]) == {"implement", "verify"}
    return [
        linear_success,
        failure_skip,
        verify_failure,
        retry_success,
        retry_exhaustion,
        _permission_pause_resume(),
        branch_join,
    ]


def main() -> int:
    cases = _reliability_cases()
    output = Path("artifacts/evaluations/dag-reliability-report.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"suite": "m24.6-dag-reliability", "cases": cases}, indent=2) + "\n",
        encoding="utf-8",
    )
    passed_count = sum(bool(case["passed"]) for case in cases)
    print(
        "dag-reliability:",
        f"passed={passed_count}/{len(cases)}",
        f"output={output}",
    )
    return 0 if passed_count == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
