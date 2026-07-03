"""Integration tests for normal orchestrator graph execution flows."""

from __future__ import annotations

import asyncio

from db.enums import TimelineEventType
from orchestrator import OrchestratorState, WorkerResult, build_orchestrator_graph
from repositories import (
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    SessionRepository,
    SessionStateRepository,
    UserRepository,
    session_scope,
)
from tests.integration.orchestrator_graph_support import SequencedWorker, StaticWorker


def _scout_json_payload(title: str = "Inspect orchestrator drift") -> dict[str, object]:
    return {
        "proposals": [
            {
                "title": title,
                "description": "Review orchestrator paths that accumulate scout output.",
                "value": "high",
                "effort": "small",
                "risk": "medium",
                "layer_impact": "orchestrator",
                "validation_path": "Run orchestrator graph execution tests.",
                "hitl_need": "optional",
                "evidence": ["orchestrator/graph.py"],
                "implementation_slice": "Persist structured scout proposals.",
            }
        ]
    }


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
        "task spec and route generated",
        "memory context loaded",
        "approval not required",
        "worker dispatched",
        "worker result received",
        "verification passed",
        "result summarized and session state updated",
        "memory persistence queued",
    ]


def _seed_graph_memory(session_factory, repo_url: str) -> tuple[str, str]:
    with session_scope(session_factory) as session:
        user = UserRepository(session).create(external_user_id="graph-memory-user")
        conversation = SessionRepository(session).create(
            user_id=user.id,
            channel="http",
            external_thread_id="thread-1",
        )
        PersonalMemoryRepository(session).upsert(
            user_id=user.id,
            memory_key="communication_style",
            value={"style": "concise"},
            source="operator",
            confidence=0.9,
            scope="global",
            requires_verification=False,
        )
        ProjectMemoryRepository(session).upsert(
            repo_url=repo_url,
            memory_key="test_command",
            value={"command": ".venv/bin/pytest tests/unit"},
            source="worker_result",
            confidence=0.8,
            scope="repo",
        )
        SessionStateRepository(session).upsert(
            session_id=conversation.id,
            active_goal="wire graph memory",
            decisions_made={"strategy": "load_all"},
            files_touched=["orchestrator/graph.py"],
        )
        user_id = user.id
        session_id = conversation.id
    return user_id, session_id


def _memory_persisting_worker() -> StaticWorker:
    result = WorkerResult(
        status="success",
        summary="Memory-aware task completed.",
        files_changed=["orchestrator/graph.py"],
        test_results=[{"name": "memory", "status": "passed"}],
        memory_to_persist=[
            {
                "category": "personal",
                "memory_key": "preferred_verification",
                "value": {"command": ".venv/bin/pytest tests/unit"},
                "source": "worker_result",
                "confidence": 0.7,
                "requires_verification": False,
            },
            {
                "category": "project",
                "memory_key": "graph_memory_path",
                "value": {"file": "orchestrator/graph.py"},
                "source": "worker_result",
                "confidence": 0.8,
            },
        ],
    )
    return StaticWorker(result)


def test_orchestrator_graph_loads_and_persists_memory(session_factory) -> None:
    """A DB-backed graph should pass loaded memory to workers and persist worker memory."""
    repo_url = "https://github.com/natanayalo/code-agent"
    user_id, session_id = _seed_graph_memory(session_factory, repo_url)
    worker = _memory_persisting_worker()
    graph = build_orchestrator_graph(worker=worker, session_factory=session_factory)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "session": {
                    "session_id": session_id,
                    "user_id": user_id,
                    "channel": "http",
                    "external_thread_id": "thread-1",
                },
                "task": {
                    "task_text": "Update memory-aware task execution",
                    "repo_url": repo_url,
                    "branch": "master",
                },
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)

    assert len(worker.requests) == 1
    memory_context = worker.requests[0].memory_context
    assert memory_context["personal"][0]["memory_key"] == "communication_style"
    assert memory_context["project"][0]["memory_key"] == "test_command"
    assert memory_context["session"]["active_goal"] == "wire graph memory"
    assert {event.event_type for event in state.timeline_events} >= {
        TimelineEventType.MEMORY_LOADED,
        TimelineEventType.MEMORY_PERSISTED,
    }
    memory_loaded_event = next(
        event
        for event in state.timeline_events
        if event.event_type == TimelineEventType.MEMORY_LOADED
    )
    assert memory_loaded_event.payload["retrieval_mode"] == "full_text"
    assert memory_loaded_event.payload["search_query"] == "Update memory-aware task execution"
    assert memory_loaded_event.payload["personal_keys"] == ["communication_style"]
    assert memory_loaded_event.payload["project_keys"] == ["test_command"]
    assert state.progress_updates[-1] == "persisted 2 memory entries"

    with session_scope(session_factory) as session:
        personal = PersonalMemoryRepository(session).get(
            user_id=user_id,
            memory_key="preferred_verification",
        )
        project = ProjectMemoryRepository(session).get(
            repo_url=repo_url,
            memory_key="graph_memory_path",
        )

    assert personal is not None
    assert personal.value == {"command": ".venv/bin/pytest tests/unit"}
    assert personal.requires_verification is False
    assert project is not None
    assert project.value == {"file": "orchestrator/graph.py"}


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


def test_orchestrator_graph_clarification_resume_token_resolution_allows_progress() -> None:
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["artifacts/review.md"],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="Done",
        )
    )
    graph = build_orchestrator_graph(worker=worker)
    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_id": "task-clar-1",
                    "task_text": "Review orchestrator and summarize findings",
                    "constraints": {
                        "interactions": {
                            "old-hash": {
                                "status": "resolved",
                                "interaction_type": "clarification",
                                "data": {
                                    "source": "task_spec",
                                    "resume_token": "clarification-task-clar-1",
                                    "questions": ["old question"],
                                },
                            }
                        }
                    },
                },
                "task_spec": {
                    "goal": "Review orchestrator and summarize findings",
                    "task_type": "investigation",
                    "risk_level": "low",
                    "delivery_mode": "workspace",
                    "allowed_actions": ["modify_workspace_files"],
                    "forbidden_actions": ["hardcode_secrets"],
                    "requires_clarification": True,
                    "clarification_questions": ["new question wording"],
                    "requires_permission": False,
                },
                "task_kind": "implementation",
                "session": {
                    "session_id": "s1",
                    "user_id": "u1",
                    "channel": "http",
                    "external_thread_id": "t1",
                },
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)
    assert state.result is not None
    assert state.result.status == "success"
    assert len(worker.requests) == 1


def test_orchestrator_graph_review_task_without_deliverable_fails() -> None:
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="Completed.",
        )
    )
    graph = build_orchestrator_graph(worker=worker)
    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": (
                        "Review the orchestrator implementation and compare to best practices"
                    ),
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)
    assert state.verification is not None
    assert state.verification.status == "failed"
    assert state.verification.failure_kind == "incomplete_delivery"
    assert state.result is not None
    assert state.result.status == "failure"


def test_orchestrator_graph_scout_task_skips_delivery() -> None:
    """Scout tasks should process normally but skip branch delivery entirely."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="Scout complete.",
            json_payload=_scout_json_payload(),
        )
    )
    graph = build_orchestrator_graph(worker=worker)
    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Scout the orchestrator module",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "constraints": {"task_type": "scout", "delivery_mode": "branch"},
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)
    assert state.task_spec is not None
    assert state.task_spec.task_type == "scout"
    assert state.task_spec.delivery_mode == "summary"
    assert state.result is not None
    assert state.result.status == "success"
    assert len(worker.requests) == 1
    assert worker.requests[0].response_format == "json"
    assert "delivery completed" not in state.progress_updates


def test_orchestrator_graph_fails_deep_scout_before_research_for_invalid_json_payload() -> None:
    """Deep Scout should not advance to research without structured repo-phase JSON."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="Scout complete without JSON.",
            json_payload=None,
        )
    )
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Deep scout the orchestrator module",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "constraints": {
                        "task_type": "scout",
                        "scout_mode": "deep",
                        "delivery_mode": "summary",
                    },
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)

    assert state.result is not None
    assert state.result.status == "failure"
    assert state.result.failure_kind == "incomplete_delivery"
    assert state.current_step == "persist_memory"
    assert len(worker.requests) == 1
    assert state.scout_phase is None
