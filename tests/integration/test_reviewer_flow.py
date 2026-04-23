"""Integration test for the advisory independent reviewer flow."""

import asyncio
import json

from orchestrator.graph import build_orchestrator_graph
from orchestrator.state import OrchestratorState
from workers import Worker, WorkerRequest, WorkerResult


class StaticWorker(Worker):
    """Test worker that returns a predefined result."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest, **kwargs) -> WorkerResult:
        self.requests.append(request)
        return self.result


def test_independent_review_flow_e2e():
    """Verify that the independent reviewer runs after verification and adds
    findings to the summary."""

    # 1. Setup mocks
    # Main worker run
    main_worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="Modified the code.",
            files_changed=["main.py"],
            diff_text="--- main.py\n+++ main.py\n+    print('hello')",
        )
    )

    # Independent reviewer (Gemini)
    review_payload = {
        "summary": "The change is good but could use a comment.",
        "confidence": 0.95,
        "outcome": "findings",
        "findings": [
            {
                "title": "Missing comment",
                "category": "documentation",
                "confidence": 0.9,
                "file_path": "main.py",
                "line_start": 1,
                "line_end": 1,
                "severity": "low",
                "why_it_matters": "Clarity is key",
            }
        ],
        "reviewer_kind": "independent_reviewer",
    }
    gemini_worker = StaticWorker(
        WorkerResult(
            status="success",
            summary=f"Review findings:\n```json\n{json.dumps(review_payload)}\n```",
        )
    )

    # 2. Build graph
    graph = build_orchestrator_graph(worker=main_worker, gemini_worker=gemini_worker)

    # 3. Initial state
    initial_input = {
        "task": {
            "task_text": "Update main.py",
            "repo_url": "https://github.com/example/repo",
            "branch": "main",
        }
    }

    # 4. Run graph
    raw_output = asyncio.run(graph.ainvoke(initial_input))
    state = OrchestratorState.model_validate(raw_output)

    # 5. Verify outcomes
    assert state.review is not None
    assert state.review.outcome == "findings"
    assert state.review.findings[0].title == "Missing comment"

    # Verify summary includes findings
    summary = state.result.summary
    assert "### Reviewer Findings" in summary
    assert "Missing comment" in summary
    assert "Modified the code." in summary

    # Verify progress updates
    assert "independent review completed" in state.progress_updates
