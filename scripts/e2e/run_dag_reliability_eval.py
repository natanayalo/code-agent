#!/usr/bin/env python3
"""Run deterministic M24.6 control-plane DAG reliability checks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

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


def _nodes(branching: bool = False) -> list[dict]:
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
    if branching:
        nodes += [
            {
                "node_id": "verify",
                "title": "Verify",
                "depends_on": ["inspect"],
                "task_spec": {"goal": "Verify"},
                "node_kind": "verify",
            },
            {
                "node_id": "join",
                "title": "Join",
                "depends_on": ["implement", "verify"],
                "task_spec": {"goal": "Join"},
                "node_kind": "verify",
            },
        ]
    return nodes


def _run(name: str, state: OrchestratorState, results: list[WorkerResult]) -> dict[str, object]:
    worker = ScenarioWorker(results)
    result, outcomes, _ = asyncio.run(_await_decomposed_nodes(state, worker))
    return {
        "scenario": name,
        "passed": result.status == "success",
        "outcomes": [{"node_id": item.node_id, "status": item.status} for item in outcomes],
        "dispatch_order": [
            request.task_text.split("Current DAG node (")[1].split(")")[0]
            for request in worker.requests
        ],
    }


def main() -> int:
    def success() -> WorkerResult:
        return WorkerResult(status="success", summary="ok")

    cases = [
        _run("linear_success", _state(_nodes()), [success(), success()]),
        _run(
            "node_failure_downstream_skip",
            _state(_nodes()),
            [
                success(),
                WorkerResult(status="failure", failure_kind="worker_failure"),
                WorkerResult(status="failure", failure_kind="worker_failure"),
                WorkerResult(status="failure", failure_kind="worker_failure"),
            ],
        ),
        _run(
            "verify_node_failure",
            _state(_nodes()),
            [
                success(),
                WorkerResult(status="failure", failure_kind="test"),
                WorkerResult(status="failure", failure_kind="test"),
                WorkerResult(status="failure", failure_kind="test"),
            ],
        ),
        _run(
            "retry_success",
            _state(_nodes()),
            [success(), WorkerResult(status="failure"), success()],
        ),
        _run(
            "retry_exhaustion",
            _state(_nodes()),
            [
                success(),
                WorkerResult(status="failure"),
                WorkerResult(status="failure"),
                WorkerResult(status="failure"),
            ],
        ),
        _run(
            "serial_branch_join", _state(_nodes(True)), [success(), success(), success(), success()]
        ),
    ]
    cases[1]["passed"] = cases[1]["outcomes"][-1]["status"] == "failed"
    cases[2]["passed"] = cases[2]["outcomes"][-1]["status"] == "failed"
    cases[4]["passed"] = cases[4]["outcomes"][-1]["status"] == "failed"
    cases[5]["passed"] = cases[5]["dispatch_order"] == ["inspect", "implement", "verify", "verify"]
    output = Path("artifacts/evaluations/dag-reliability-report.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"suite": "m24.6-dag-reliability", "cases": cases}, indent=2) + "\n"
    )
    return 0 if all(case["passed"] for case in cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
