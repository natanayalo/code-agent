"""Orchestrator-path evaluation runner for the frozen suite."""

from __future__ import annotations

import json
import logging

from evaluation.harness import (
    EvaluationRunner,
    FrozenTaskCase,
    ReviewOutcome,
    WorkerOutcome,
    normalize_path_for_scoring,
)
from orchestrator import OrchestratorState, build_orchestrator_graph
from orchestrator.checkpoints import create_in_memory_checkpointer
from orchestrator.task_spec import is_destructive_task
from workers import ArtifactReference, TestResult, Worker, WorkerRequest, WorkerResult

logger = logging.getLogger(__name__)


class _FrozenOutcomeWorker(Worker):
    """Worker adapter that serves deterministic per-case outcomes."""

    def __init__(self, outcomes_by_case_id: dict[str, WorkerOutcome]) -> None:
        self._outcomes_by_case_id = dict(outcomes_by_case_id)

    async def run(
        self,
        request: WorkerRequest,
        *,
        system_prompt: str | None = None,
    ) -> WorkerResult:
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

        if request.task_text == "Perform an independent review of the changes.":
            # Mock review generation based on the deterministic ReviewOutcome if available.
            review = outcome.review
            if review is None:
                review_payload = {
                    "reviewer_kind": "independent_reviewer",
                    "summary": "Mock review: no findings.",
                    "confidence": 1.0,
                    "outcome": "no_findings",
                    "findings": [],
                }
            else:
                findings = []
                # Generate mock actionable findings (high confidence)
                for i in range(review.actionable_findings_count):
                    findings.append(
                        {
                            "severity": "high",
                            "category": "logic",
                            "confidence": 0.9,
                            "file_path": f"file_{i}.py",
                            "line_start": 10 + i,
                            "title": f"Mock Actionable Finding {i}",
                            "why_it_matters": "critical logic issue",
                        }
                    )
                # Generate mock false positive findings (low confidence to trigger suppression)
                for i in range(review.false_positive_findings_count):
                    findings.append(
                        {
                            "severity": "low",
                            "category": "style",
                            "confidence": 0.1,
                            "file_path": f"style_{i}.py",
                            "line_start": 100 + i,
                            "title": f"Mock False Positive {i}",
                            "why_it_matters": "style issue",
                        }
                    )
                review_payload = {
                    "reviewer_kind": "independent_reviewer",
                    "summary": f"Mock review with {len(findings)} findings.",
                    "confidence": 1.0,
                    "outcome": "findings" if findings else "no_findings",
                    "findings": findings,
                }
            return WorkerResult(
                status="success",
                summary=json.dumps(review_payload),
                commands_run=[],
                files_changed=[],
                test_results=[],
                artifacts=[
                    ArtifactReference(name="workspace", uri="file:///tmp/mock-eval-workspace")
                ],
                next_action_hint="summarize_result",
            )

        return WorkerResult(
            status=outcome.status,
            summary=outcome.summary,
            commands_run=[],
            files_changed=list(outcome.files_changed),
            test_results=test_results,
            artifacts=[ArtifactReference(name="workspace", uri="file:///tmp/mock-eval-workspace")],
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

    async def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
        constraints: dict[str, object] = {
            "evaluation_case_id": case.case_id,
            "execution_mode": "unattended",
        }
        # Frozen eval runs unattended. Pre-seed trusted approval for non-destructive
        # cases so high-risk task-spec gating does not block deterministic scoring.
        if not is_destructive_task(case.task_text, constraints):
            constraints["approval"] = {
                "status": "approved",
                "source": "orchestrator",
                "reason": "Frozen evaluation unattended non-destructive task auto-approved.",
            }

        raw_state = await self._graph.ainvoke(
            {
                "task": {
                    "task_text": case.task_text,
                    "repo_url": f"https://example.invalid/{case.repo_fixture}",
                    "branch": "master",
                    "worker_override": self._worker_override,
                    "constraints": constraints,
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
        review_outcome: ReviewOutcome | None = None
        if state.review is not None:
            # Suppressed findings are a pragmatic proxy for filtered/rejected findings in this path;
            # this is not a strict semantic "incorrect finding" measurement.
            actionable_fingerprint_set = {
                (
                    normalize_path_for_scoring(finding.file_path),
                    finding.title,
                    finding.line_start,
                    finding.line_end,
                )
                for finding in state.review.findings
            }
            suppressed_fingerprint_set = {
                (
                    normalize_path_for_scoring(suppressed.finding.file_path),
                    suppressed.finding.title,
                    suppressed.finding.line_start,
                    suppressed.finding.line_end,
                )
                for suppressed in state.review.suppressed_findings
            }
            overlapping_fingerprints = actionable_fingerprint_set & suppressed_fingerprint_set
            if overlapping_fingerprints:
                logger.warning(
                    "Independent review output contained overlapping actionable and suppressed "
                    "findings; deduplicating by fingerprint with suppression precedence."
                )
            # "Actionable" in this runner currently means "not suppressed by policy filtering".
            actionable_findings_count = len(actionable_fingerprint_set - suppressed_fingerprint_set)
            false_positive_findings_count = len(suppressed_fingerprint_set)
            # Total findings are deduplicated across actionable/suppressed sets.
            total_findings_count = actionable_findings_count + false_positive_findings_count
            repair_attempted = state.repair_handoff_requested
            review_outcome = ReviewOutcome(
                findings_count=total_findings_count,
                actionable_findings_count=actionable_findings_count,
                false_positive_findings_count=false_positive_findings_count,
                fix_after_review_attempted=bool(repair_attempted),
                fix_after_review_succeeded=(
                    repair_attempted
                    and state.verification is not None
                    and state.verification.status == "passed"
                )
                if repair_attempted
                else None,
            )

        return WorkerOutcome(
            status=result.status,
            summary=result.summary or "",
            files_changed=tuple(result.files_changed),
            tests_passed=tests_passed,
            review=review_outcome,
        )
