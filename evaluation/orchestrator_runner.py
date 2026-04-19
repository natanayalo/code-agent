"""Orchestrator-path evaluation runner for the frozen suite."""

from __future__ import annotations

import asyncio

from evaluation.harness import EvaluationRunner, FrozenTaskCase, WorkerOutcome
from orchestrator import OrchestratorState, build_orchestrator_graph
from orchestrator.checkpoints import create_in_memory_checkpointer
from workers import TestResult, Worker, WorkerRequest, WorkerResult


class _FrozenOutcomeWorker(Worker):
    """Worker adapter that serves deterministic per-case outcomes."""

    def __init__(self, outcomes_by_case_id: dict[str, WorkerOutcome]) -> None:
        self._outcomes_by_case_id = dict(outcomes_by_case_id)

    async def run(self, request: WorkerRequest) -> WorkerResult:
        case_id_raw = request.constraints.get("evaluation_case_id")
        case_id = case_id_raw if isinstance(case_id_raw, str) else ""
        outcome = self._outcomes_by_case_id.get(case_id)
        if outcome is None:
            return WorkerResult(
                status="failure",
                summary=f"Missing replay outcome for case '{case_id or 'unknown'}'.",
                commands_run=[],
                files_changed=[],
                test_results=[],
                artifacts=[],
            )

        if outcome.tests_passed is None:
            test_results: list[TestResult] = []
        else:
            test_results = [
                TestResult(
                    name="frozen-eval",
                    status="passed" if outcome.tests_passed else "failed",
                    details="deterministic replay outcome",
                )
            ]

        return WorkerResult(
            status=outcome.status,
            summary=outcome.summary,
            commands_run=[],
            files_changed=list(outcome.files_changed),
            test_results=test_results,
            artifacts=[],
            next_action_hint="persist_memory",
        )


class OrchestratorReplayRunner(EvaluationRunner):
    """Execute frozen-suite cases through the real orchestrator graph path."""

    def __init__(
        self,
        outcomes_by_case_id: dict[str, WorkerOutcome],
        *,
        worker_override: str = "codex",
    ) -> None:
        self._worker = _FrozenOutcomeWorker(outcomes_by_case_id=outcomes_by_case_id)
        self._worker_override = worker_override
        self._graph = build_orchestrator_graph(
            worker=self._worker,
            gemini_worker=self._worker,
            checkpointer=create_in_memory_checkpointer(),
        )

    def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
        return asyncio.run(self._run_case_async(case))

    async def _run_case_async(self, case: FrozenTaskCase) -> WorkerOutcome:
        raw_state = await self._graph.ainvoke(
            {
                "task": {
                    "task_text": case.task_text,
                    "repo_url": f"https://example.invalid/{case.repo_fixture}",
                    "branch": "main",
                    "worker_override": self._worker_override,
                    "constraints": {
                        "evaluation_case_id": case.case_id,
                        "execution_mode": "unattended",
                    },
                    "budget": {
                        "max_iterations": 1,
                        "max_tool_calls": 0,
                        "max_shell_commands": 0,
                        "worker_timeout_seconds": 30,
                        "orchestrator_timeout_seconds": 35,
                    },
                }
            },
            config={"configurable": {"thread_id": f"frozen-eval-{case.case_id}"}},
        )
        if "__interrupt__" in raw_state:
            return WorkerOutcome(
                status="failure",
                summary=(
                    "Orchestrator execution was interrupted awaiting approval in "
                    "unattended evaluation mode."
                ),
                files_changed=(),
                tests_passed=False,
            )
        state = OrchestratorState.model_validate(raw_state)
        result = state.result
        if result is None:
            return WorkerOutcome(
                status="failure",
                summary="Orchestrator returned without a worker result.",
                files_changed=(),
                tests_passed=False,
            )

        if not result.test_results:
            tests_passed: bool | None = None
        else:
            tests_passed = all(
                test_result.status == "passed" for test_result in result.test_results
            )

        return WorkerOutcome(
            status=result.status,
            summary=result.summary or "",
            files_changed=tuple(result.files_changed),
            tests_passed=tests_passed,
        )
