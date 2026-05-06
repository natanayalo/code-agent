"""Integration tests for the LangGraph orchestrator skeleton."""

from __future__ import annotations

import asyncio
from pathlib import Path

from langgraph.types import Command

from orchestrator import OrchestratorState, WorkerResult, build_orchestrator_graph
from orchestrator.checkpoints import create_async_sqlite_checkpointer
from workers import Worker, WorkerProfile, WorkerRequest


class StaticWorker(Worker):
    """Test worker that returns a predefined result and records requests."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        return self.result


class SequencedWorker(Worker):
    """Test worker that yields a predefined sequence of results."""

    def __init__(self, results: list[WorkerResult]) -> None:
        self._results = list(results)
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        if not self._results:
            raise AssertionError("SequencedWorker received more requests than expected.")
        return self._results.pop(0)


class UnexpectedWorker(Worker):
    """Test worker that should never be invoked."""

    def __init__(self, message: str) -> None:
        self.message = message

    async def run(self, request: WorkerRequest) -> WorkerResult:
        raise AssertionError(self.message)


class SlowWorker(Worker):
    """Test worker that can be timed out or cancelled by the orchestrator."""

    def __init__(self, *, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.requests: list[WorkerRequest] = []
        self.cancelled = False

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        try:
            await asyncio.sleep(self.delay_seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return WorkerResult(
            status="success",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="Slow worker finished.",
        )


class CrashingWorker(Worker):
    """Test worker that raises an unexpected exception before returning a result."""

    def __init__(self, message: str = "worker crashed") -> None:
        self.message = message
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        raise RuntimeError(self.message)


class CleanupCrashingWorker(Worker):
    """Test worker that raises during cancellation cleanup."""

    def __init__(self, *, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.requests: list[WorkerRequest] = []
        self.cleanup_failed = False

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        try:
            await asyncio.sleep(self.delay_seconds)
        except asyncio.CancelledError as exc:
            self.cleanup_failed = True
            raise RuntimeError("cleanup failed after cancellation") from exc
        return WorkerResult(
            status="success",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="Cleanup worker finished.",
        )


def test_orchestrator_graph_runs_happy_path_with_fake_worker() -> None:
    """The compiled graph should complete the documented happy-path node sequence."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["orchestrator/graph.py"],
            test_results=[{"name": "fake-worker", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Add generic webhook endpoint",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert state.normalized_task_text == "Add generic webhook endpoint"
    assert state.task_kind == "implementation"
    assert state.task_spec is not None
    assert state.task_spec.goal == "Add generic webhook endpoint"
    assert state.route.chosen_worker == "codex"
    assert state.approval.required is False
    assert state.approval.status == "not_required"
    assert state.dispatch.worker_type == "codex"
    assert state.dispatch.run_id is None
    assert state.dispatch.workspace_id is None
    assert len(worker.requests) == 1
    assert worker.requests[0].task_text == "Add generic webhook endpoint"
    assert worker.requests[0].repo_url == "https://github.com/natanayalo/code-agent"
    assert worker.requests[0].branch == "master"
    assert worker.requests[0].task_spec is not None
    assert worker.requests[0].task_spec["goal"] == "Add generic webhook endpoint"
    assert state.result is not None
    assert state.result.status == "success"
    assert state.result.summary == "codex finished with status success"
    assert state.result.test_results[0].status == "passed"
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "planning skipped: task is straightforward",
        "task spec generated",
        "memory context loaded",
        "worker selected: codex (reason: cheap_mechanical_change)",
        "approval not required",
        "worker dispatched",
        "worker result received",
        "verification passed",
        "result summarized and session state updated",
        "memory persistence queued",
    ]


def test_orchestrator_graph_runs_one_verifier_repair_handoff_then_stops() -> None:
    worker = SequencedWorker(
        [
            WorkerResult(
                status="success",
                summary="Initial implementation finished.",
                commands_run=[],
                files_changed=["orchestrator/graph.py"],
                test_results=[{"name": "unit", "status": "failed"}],
                artifacts=[],
                next_action_hint="persist_memory",
            ),
            WorkerResult(
                status="success",
                summary="Applied verifier repair follow-up.",
                commands_run=[],
                files_changed=["orchestrator/graph.py"],
                test_results=[{"name": "unit", "status": "failed"}],
                artifacts=[],
                next_action_hint="persist_memory",
            ),
        ]
    )
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Fix verifier repair behavior",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert len(worker.requests) == 2
    assert worker.requests[1].task_text.startswith(
        "Apply targeted code fixes for failed verification checks."
    )
    assert state.verification is not None
    assert state.verification.status == "failed"
    assert state.result is not None
    assert state.result.next_action_hint == "await_manual_follow_up"
    assert "Verification is still failing after 1 bounded repair attempt" in (
        state.result.summary or ""
    )
    assert any(
        "verification failed; queued bounded repair handoff (1/1)" in update
        for update in state.progress_updates
    )
    assert any(
        "verification failed after bounded repair attempts" in update
        for update in state.progress_updates
    )


def test_orchestrator_graph_resumes_from_persisted_sqlite_checkpoint(
    tmp_path: Path,
) -> None:
    """An interrupted graph can resume from a persisted SQLite checkpoint."""

    async def scenario() -> None:
        checkpoint_path = tmp_path / "orchestrator-checkpoints.sqlite"
        config = {"configurable": {"thread_id": "task-021"}}
        initial_input = {
            "task": {
                "task_text": "Add checkpoint persistence",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "master",
            }
        }

        unexpected_worker = UnexpectedWorker("await_result should not execute before resume.")

        async with create_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            interrupted_graph = build_orchestrator_graph(
                worker=unexpected_worker,
                checkpointer=checkpointer,
                interrupt_before=["await_result"],
            )

            await interrupted_graph.ainvoke(initial_input, config=config)
            snapshot = await interrupted_graph.aget_state(config)

        assert snapshot.next == ("await_result",)
        assert snapshot.values["current_step"] == "dispatch_job"
        assert snapshot.values["approval"]["status"] == "not_required"
        assert snapshot.values["dispatch"]["worker_type"] == "codex"
        assert snapshot.values.get("result") is None
        assert snapshot.values["progress_updates"] == [
            "task ingested",
            "task classified as implementation",
            "planning skipped: task is straightforward",
            "task spec generated",
            "memory context loaded",
            "worker selected: codex (reason: cheap_mechanical_change)",
            "approval not required",
            "worker dispatched",
        ]

        resumed_worker = StaticWorker(
            WorkerResult(
                status="success",
                commands_run=[],
                files_changed=["orchestrator/graph.py"],
                test_results=[{"name": "checkpoint-resume", "status": "passed"}],
                artifacts=[],
                next_action_hint="persist_memory",
                summary=None,
            )
        )
        async with create_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            resumed_graph = build_orchestrator_graph(
                worker=resumed_worker,
                checkpointer=checkpointer,
            )

            resumed_snapshot = await resumed_graph.aget_state(config)
            raw_output = await resumed_graph.ainvoke(None, config=config)

        assert resumed_snapshot.next == ("await_result",)

        state = OrchestratorState.model_validate(raw_output)

        assert state.current_step == "persist_memory"
        assert state.normalized_task_text == "Add checkpoint persistence"
        assert state.task_kind == "implementation"
        assert state.route.chosen_worker == "codex"
        assert state.approval.status == "not_required"
        assert state.dispatch.worker_type == "codex"
        assert state.dispatch.run_id is None
        assert state.dispatch.workspace_id is None
        assert len(resumed_worker.requests) == 1
        assert resumed_worker.requests[0].task_text == "Add checkpoint persistence"
        assert state.result is not None
        assert state.result.status == "success"
        assert state.result.summary == "codex finished with status success"
        assert state.result.test_results[0].status == "passed"
        assert state.progress_updates == [
            "task ingested",
            "task classified as implementation",
            "planning skipped: task is straightforward",
            "task spec generated",
            "memory context loaded",
            "worker selected: codex (reason: cheap_mechanical_change)",
            "approval not required",
            "worker dispatched",
            "worker result received",
            "verification passed",
            "result summarized and session state updated",
            "memory persistence queued",
        ]

    asyncio.run(scenario())


def test_orchestrator_graph_errors_when_selected_worker_is_unavailable() -> None:
    """A manual override for an unconfigured worker must fail explicitly, not silently fall back."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["workers/codex_worker.py"],
            test_results=[{"name": "unexpected-worker-call", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run a task with explicit gemini override",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "worker_override": "gemini",
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert state.route.chosen_worker == "gemini"
    assert state.route.route_reason == "runtime_unavailable"
    assert state.route.override_applied is True
    assert state.dispatch.worker_type == "gemini"
    assert state.dispatch.run_id is None
    assert state.dispatch.workspace_id is None
    assert worker.requests == []
    assert state.result is not None
    assert state.result.status == "failure"
    assert (
        state.result.summary
        == "No worker is available for route 'gemini'. Available workers: codex."
    )
    assert state.result.next_action_hint == "configure_requested_worker"
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "planning skipped: task is straightforward",
        "task spec generated",
        "memory context loaded",
        "worker selected: gemini (reason: runtime_unavailable)",
        "approval not required",
        "worker dispatched",
        "worker unavailable: gemini",
        "verification failed",
        "result summarized and session state updated",
        "memory persistence queued",
    ]


def test_orchestrator_graph_errors_when_selected_profile_is_unavailable() -> None:
    """A manual profile override must fail explicitly when profile-aware routing is enabled."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["workers/codex_worker.py"],
            test_results=[{"name": "unexpected-worker-call", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )
    graph = build_orchestrator_graph(
        worker=worker,
        enable_worker_profiles=True,
        worker_profiles={
            "codex-native-executor": WorkerProfile(
                name="codex-native-executor",
                worker_type="codex",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
            )
        },
    )

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run a task with explicit openrouter profile override",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "worker_profile_override": "openrouter-tool-loop-legacy",
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert state.route.chosen_profile == "openrouter-tool-loop-legacy"
    assert state.route.route_reason == "runtime_unavailable"
    assert state.route.override_applied is True
    assert state.dispatch.worker_type is None
    assert state.dispatch.worker_profile == "openrouter-tool-loop-legacy"
    assert worker.requests == []
    assert state.result is not None
    assert state.result.status == "failure"
    assert (
        state.result.summary
        == "No routable worker profile is available for route 'openrouter-tool-loop-legacy'. "
        "Available profiles: codex-native-executor."
    )
    assert state.result.next_action_hint == "configure_requested_worker_profile"


def test_orchestrator_graph_worker_override_respects_profile_opt_in() -> None:
    """Worker overrides should still fail when profile-aware routing has no matching profile."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["workers/openrouter_worker.py"],
            test_results=[{"name": "unexpected-worker-call", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )
    openrouter_worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["workers/openrouter_worker.py"],
            test_results=[{"name": "unexpected-openrouter-call", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )
    graph = build_orchestrator_graph(
        worker=worker,
        openrouter_worker=openrouter_worker,
        enable_worker_profiles=True,
        worker_profiles={
            "codex-native-executor": WorkerProfile(
                name="codex-native-executor",
                worker_type="codex",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
            )
        },
    )

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run a task with explicit openrouter worker override",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "worker_override": "openrouter",
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)

    assert state.route.chosen_worker == "openrouter"
    assert state.route.route_reason == "runtime_unavailable"
    assert state.result is not None
    assert state.result.status == "failure"
    assert (
        state.result.summary
        == "No routable worker profile is available for worker route 'openrouter'. "
        "Available profiles: codex-native-executor."
    )
    assert worker.requests == []
    assert openrouter_worker.requests == []


def test_orchestrator_graph_profile_override_incompatible_with_constraints() -> None:
    """Incompatible profile overrides should report a profile-specific routing error."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["workers/codex_worker.py"],
            test_results=[{"name": "unexpected-worker-call", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )
    graph = build_orchestrator_graph(
        worker=worker,
        enable_worker_profiles=True,
        worker_profiles={
            "codex-native-executor": WorkerProfile(
                name="codex-native-executor",
                worker_type="codex",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
                mutation_policy="patch_allowed",
            ),
            "codex-read-only-executor": WorkerProfile(
                name="codex-read-only-executor",
                worker_type="codex",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace"],
                mutation_policy="read_only",
            ),
        },
    )

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run task with read-only constraint and explicit codex profile",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "worker_profile_override": "codex-native-executor",
                    "constraints": {"read_only": True},
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)

    assert state.route.chosen_profile == "codex-native-executor"
    assert state.route.route_reason == "incompatible_profile"
    assert state.result is not None
    assert state.result.status == "failure"
    assert (
        state.result.summary
        == "No routable worker profile is available for route 'codex-native-executor'. "
        "Available profiles: codex-native-executor, codex-read-only-executor."
    )
    assert state.result.next_action_hint == "configure_requested_worker_profile"
    assert worker.requests == []


def test_orchestrator_graph_interrupts_for_approval_and_resumes_cleanly(
    tmp_path: Path,
) -> None:
    """A destructive task should pause for approval and resume on confirmation."""

    async def scenario() -> None:
        checkpoint_path = tmp_path / "approval-checkpoints.sqlite"
        config = {"configurable": {"thread_id": "task-022-approved"}}
        initial_input = {
            "task": {
                "task_id": "task-022",
                "task_text": "Delete files from the repo workspace",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "master",
                "constraints": {
                    "requires_approval": True,
                    "approval_reason": "Task deletes files from the task workspace.",
                },
            }
        }

        worker = StaticWorker(
            WorkerResult(
                status="success",
                commands_run=[],
                files_changed=["sandbox/workspace.py"],
                test_results=[{"name": "approval-resume", "status": "passed"}],
                artifacts=[],
                next_action_hint="persist_memory",
                summary=None,
            )
        )
        async with create_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            graph = build_orchestrator_graph(
                worker=worker,
                checkpointer=checkpointer,
            )

            interrupted_output = await graph.ainvoke(initial_input, config=config)
            snapshot = await graph.aget_state(config)

            interrupts = interrupted_output["__interrupt__"]
            assert len(interrupts) == 1
            interrupt_payload = getattr(interrupts[0], "value")
            assert interrupt_payload["approval_type"] == "destructive_action"
            assert interrupt_payload["reason"] == "Task deletes files from the task workspace."
            assert interrupt_payload["resume_token"] == "approval-task-022"
            assert interrupt_payload["task_text"] == "Delete files from the repo workspace"
            assert snapshot.next == ("await_approval",)
            assert snapshot.values["current_step"] == "check_approval"
            assert snapshot.values["approval"]["status"] == "pending"
            assert snapshot.values["progress_updates"] == [
                "task ingested",
                "task classified as implementation",
                "planning skipped: task is straightforward",
                "task spec generated",
                "memory context loaded",
                "worker selected: codex (reason: cheap_mechanical_change)",
                "approval requested",
            ]

            raw_output = await graph.ainvoke(Command(resume=True), config=config)

        state = OrchestratorState.model_validate(raw_output)

        assert state.current_step == "persist_memory"
        assert state.approval.required is True
        assert state.approval.status == "approved"
        assert state.dispatch.worker_type == "codex"
        assert state.dispatch.run_id is None
        assert state.dispatch.workspace_id is None
        assert len(worker.requests) == 1
        assert worker.requests[0].task_text == "Delete files from the repo workspace"
        assert state.result is not None
        assert state.result.status == "success"
        assert state.result.test_results[0].name == "approval-resume"
        assert state.progress_updates == [
            "task ingested",
            "task classified as implementation",
            "planning skipped: task is straightforward",
            "task spec generated",
            "memory context loaded",
            "worker selected: codex (reason: cheap_mechanical_change)",
            "approval requested",
            "approval granted",
            "worker dispatched",
            "worker result received",
            "verification passed",
            "result summarized and session state updated",
            "memory persistence queued",
        ]

    asyncio.run(scenario())


def test_orchestrator_graph_stops_when_approval_is_rejected(tmp_path: Path) -> None:
    """A rejected destructive task should not dispatch the worker."""

    async def scenario() -> None:
        checkpoint_path = tmp_path / "approval-rejected.sqlite"
        config = {"configurable": {"thread_id": "task-022-rejected"}}
        initial_input = {
            "task": {
                "task_id": "task-022-rejected",
                "task_text": "Delete files from the repo workspace",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "master",
                "constraints": {"requires_approval": True},
            }
        }

        unexpected_worker = UnexpectedWorker("dispatch should not run after approval is rejected.")

        async with create_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            graph = build_orchestrator_graph(
                worker=unexpected_worker,
                checkpointer=checkpointer,
            )

            await graph.ainvoke(initial_input, config=config)
            raw_output = await graph.ainvoke(Command(resume=False), config=config)

        state = OrchestratorState.model_validate(raw_output)

        assert state.current_step == "persist_memory"
        assert state.approval.required is True
        assert state.approval.status == "rejected"
        assert state.dispatch.run_id is None
        assert state.result is not None
        assert state.result.status == "failure"
        assert (
            state.result.summary
            == "Task halted because the requested destructive action was not approved."
        )
        assert state.progress_updates == [
            "task ingested",
            "task classified as implementation",
            "planning skipped: task is straightforward",
            "task spec generated",
            "memory context loaded",
            "worker selected: codex (reason: cheap_mechanical_change)",
            "approval requested",
            "approval rejected",
            "result summarized and session state updated",
            "memory persistence queued",
        ]

    asyncio.run(scenario())


def test_orchestrator_graph_halts_when_clarification_is_required() -> None:
    """Clarification-gated TaskSpecs should stop before worker selection and dispatch."""
    worker = UnexpectedWorker("worker should not run while clarification is pending.")
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "fix it",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert state.task_spec is not None
    assert state.task_spec.requires_clarification is True
    assert state.route.chosen_worker is None
    assert state.dispatch.worker_type is None
    assert state.result is not None
    assert state.result.status == "failure"
    assert "pending clarification" in (state.result.summary or "")
    assert state.result.next_action_hint == "await_manual_follow_up"
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "planning skipped: task is straightforward",
        "clarification required before execution",
        "result summarized and session state updated",
        "memory persistence queued",
    ]


def test_orchestrator_graph_returns_a_structured_timeout_result() -> None:
    """The outer orchestrator timeout should fail safely instead of hanging forever."""
    worker = SlowWorker(delay_seconds=5)
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run the slow worker path",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "budget": {"orchestrator_timeout_seconds": 1},
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert len(worker.requests) == 1
    assert worker.cancelled is True
    assert state.result is not None
    assert state.result.status == "failure"
    assert state.result.summary == (
        "Worker execution exceeded the orchestrator timeout envelope (1s) and was cancelled."
    )
    assert state.result.next_action_hint == "inspect_workspace_artifacts"
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "planning skipped: task is straightforward",
        "task spec generated",
        "memory context loaded",
        "worker selected: codex (reason: cheap_mechanical_change)",
        "approval not required",
        "worker dispatched",
        "worker timed out after 1s",
        "verification failed",
        "result summarized and session state updated",
        "memory persistence queued",
    ]


def test_orchestrator_graph_surfaces_worker_cancellation_as_a_result() -> None:
    """Cancelling the graph during worker execution should still produce a typed failure."""

    async def scenario() -> None:
        worker = SlowWorker(delay_seconds=5)
        graph = build_orchestrator_graph(worker=worker)

        graph_task = asyncio.create_task(
            graph.ainvoke(
                {
                    "task": {
                        "task_text": "Cancel the worker path",
                        "repo_url": "https://github.com/natanayalo/code-agent",
                        "branch": "master",
                    }
                }
            )
        )

        for _ in range(100):
            if worker.requests:
                break
            await asyncio.sleep(0.01)
        assert worker.requests, "Worker never started before cancellation."

        graph_task.cancel()
        raw_output = await graph_task
        state = OrchestratorState.model_validate(raw_output)

        assert state.current_step == "persist_memory"
        assert worker.cancelled is True
        assert state.result is not None
        assert state.result.status == "failure"
        assert state.result.summary == (
            "Worker execution was cancelled before it returned a result."
        )
        assert state.result.next_action_hint == "await_manual_follow_up"
        assert state.progress_updates == [
            "task ingested",
            "task classified as implementation",
            "planning skipped: task is straightforward",
            "task spec generated",
            "memory context loaded",
            "worker selected: codex (reason: cheap_mechanical_change)",
            "approval not required",
            "worker dispatched",
            "worker execution cancelled",
            "verification failed",
            "result summarized and session state updated",
            "memory persistence queued",
        ]

    asyncio.run(scenario())


def test_orchestrator_graph_returns_a_structured_error_for_worker_crashes() -> None:
    """Unexpected worker exceptions should not crash the orchestrator graph."""
    worker = CrashingWorker("adapter exploded")
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run the crashing worker path",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert len(worker.requests) == 1
    assert state.result is not None
    assert state.result.status == "error"
    assert state.result.summary == (
        "Worker execution crashed unexpectedly: RuntimeError: adapter exploded"
    )
    assert state.result.next_action_hint == "inspect_worker_configuration"
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "planning skipped: task is straightforward",
        "task spec generated",
        "memory context loaded",
        "worker selected: codex (reason: cheap_mechanical_change)",
        "approval not required",
        "worker dispatched",
        "worker crashed unexpectedly",
        "verification failed",
        "result summarized and session state updated",
        "memory persistence queued",
    ]


def test_orchestrator_graph_timeout_path_tolerates_cleanup_exceptions() -> None:
    """A worker that fails while processing cancellation should not crash cleanup."""
    worker = CleanupCrashingWorker(delay_seconds=5)
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run the cleanup-crashing worker path",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "budget": {"orchestrator_timeout_seconds": 1},
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert len(worker.requests) == 1
    assert worker.cleanup_failed is True
    assert state.result is not None
    assert state.result.status == "failure"
    assert state.result.summary == (
        "Worker execution exceeded the orchestrator timeout envelope (1s) and was cancelled."
    )
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "planning skipped: task is straightforward",
        "task spec generated",
        "memory context loaded",
        "worker selected: codex (reason: cheap_mechanical_change)",
        "approval not required",
        "worker dispatched",
        "worker timed out after 1s",
        "verification failed",
        "result summarized and session state updated",
        "memory persistence queued",
    ]
