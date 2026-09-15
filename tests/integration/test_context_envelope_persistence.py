"""Integration tests for ContextEnvelope lifecycle, delivery, and persistence."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import orchestrator.temporal.activities as activities_module
from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from db.base import utc_now
from db.enums import ArtifactType, ExecutionPlanNodeStatus
from db.models import (
    Artifact,
    Base,
    ExecutionPlan,
    ExecutionPlanNode,
    ExecutionPlanNodeAttempt,
    Task,
    User,
    WorkerRun,
)
from db.models import Session as DBSession
from orchestrator.context_envelope import _compute_digests, assemble_context_envelope
from orchestrator.execution import TaskExecutionService
from orchestrator.execution_outcome_service import (
    _build_artifact_index,
    _persist_artifacts_for_run,
)
from orchestrator.graph import _effective_input_evidence, build_await_result_node
from orchestrator.node_execution import NodeActivityRequest, logical_activity_key
from orchestrator.state import (
    DecomposedTaskNode,
    DecomposedTaskPlan,
    MemoryContext,
    MemoryEntry,
    OrchestratorState,
    RouteDecision,
    TaskSpec,
    WorkerDispatch,
)
from orchestrator.temporal.activities import (
    TaskExecutionActivities,
    _project_decomposed_runtime_manifest,
)
from repositories import TemporalTaskStateRepository, session_scope
from repositories.sqlalchemy_run import ArtifactRepository
from workers.base import ArtifactReference, Worker, WorkerRequest, WorkerResult


class StubWorker(Worker):
    """Stub worker recording requests received and returning mock results."""

    def __init__(self, status: str = "success", failure_kind: str | None = None) -> None:
        self.status = status
        self.failure_kind = failure_kind
        self.last_request: WorkerRequest | None = None

    def available_workers(self) -> dict[str, Any]:
        return {"antigravity": self, "codex": self}

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.last_request = request
        return WorkerResult(
            status=self.status,
            failure_kind=self.failure_kind,
            summary=(
                "Worker completed with meaningful output that passes validation threshold easily."
            ),
            artifacts=[
                ArtifactReference(
                    name="test_log.txt",
                    uri="file:///tmp/test_log.txt",
                    artifact_type="log",
                )
            ],
        )


@pytest.fixture
def db_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_db_run(session_factory: sessionmaker[Session]) -> tuple[str, str]:
    with session_factory() as session:
        user = User(external_user_id="u1", display_name="Operator")
        session.add(user)
        session.flush()

        db_session = DBSession(user_id=user.id, channel="web", external_thread_id="t1")
        session.add(db_session)
        session.flush()

        task = Task(
            session_id=db_session.id,
            task_text="Run task",
            priority=0,
            status="in_progress",
        )
        session.add(task)
        session.flush()

        now = utc_now()
        run = WorkerRun(
            task_id=task.id,
            session_id=db_session.id,
            worker_type="antigravity",
            status="running",
            started_at=now,
            commands_run=[],
            files_changed=[],
            artifact_index=[],
        )
        session.add(run)
        session.commit()
        return task.id, run.id


def _seed_decomposed_task_and_plan(
    session_factory: sessionmaker[Session],
    task_id: str = "task-decomp-1",
    num_nodes: int = 2,
) -> tuple[str, str]:
    """Seed a task, execution plan, and plan nodes for durable DAG execution."""
    with session_factory() as session:
        user = User(external_user_id="u1", display_name="Operator")
        session.add(user)
        session.flush()

        db_session = DBSession(user_id=user.id, channel="web", external_thread_id="t1")
        session.add(db_session)
        session.flush()

        task = Task(
            id=task_id,
            session_id=db_session.id,
            task_text="Decomposed workflow task",
            priority=0,
            status="in_progress",
        )
        session.add(task)
        session.flush()

        plan = ExecutionPlan(task_id=task.id)
        session.add(plan)
        session.flush()

        node_step1 = ExecutionPlanNode(
            plan_id=plan.id,
            node_id="step1",
            sequence_number=0,
            goal="Do step 1",
            acceptance_criteria="AC1",
            node_kind="inspect",
            task_spec={"goal": "Do step 1", "acceptance_criteria": ["AC1"]},
            status=ExecutionPlanNodeStatus.PENDING,
        )
        session.add(node_step1)
        if num_nodes >= 2:
            node_step2 = ExecutionPlanNode(
                plan_id=plan.id,
                node_id="step2",
                sequence_number=1,
                goal="Do step 2",
                acceptance_criteria="AC2",
                node_kind="inspect",
                task_spec={"goal": "Do step 2", "acceptance_criteria": ["AC2"]},
                depends_on=["step1"],
                status=ExecutionPlanNodeStatus.PENDING,
            )
            session.add(node_step2)
        session.commit()
        return task.id, plan.id


@pytest.mark.asyncio
async def test_await_result_node_delivers_and_attaches_envelope(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Verify context_envelope is delivered pre-dispatch and attached to result."""
    worker = StubWorker(status="success")
    await_node = build_await_result_node(
        worker,
        available_profile_names=frozenset(),
        session_factory=db_session_factory,
        context_envelope_enabled=True,
    )

    mem = MemoryContext()
    mem.project.append(
        MemoryEntry(
            memory_key="arch",
            value={"pattern": "pipeline"},
            gate_status="accepted",
        )
    )

    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "task-env-1", "task_text": "Do envelope test"},
            "session": {
                "session_id": "sess-env-1",
                "user_id": "u1",
                "channel": "web",
                "external_thread_id": "t1",
            },
            "dispatch": WorkerDispatch(worker_type="antigravity", workspace_id="ws-123"),
            "route": RouteDecision(chosen_worker="antigravity"),
            "task_spec": TaskSpec(goal="Do envelope test", acceptance_criteria=["AC1"]),
            "memory": mem,
        }
    )

    response = await await_node(state)
    result_data = response["result"]

    # 1. Worker received request with populated context_envelope
    assert worker.last_request is not None
    assert worker.last_request.context_envelope is not None
    assert worker.last_request.context_envelope["schema_version"] == 1
    assert worker.last_request.context_envelope["objective"] == "Do envelope test"
    assert worker.last_request.context_envelope["context_content_digest"]

    # 2. Result contains retained context_envelope artifact
    artifacts = result_data["artifacts"]
    envelope_arts = [a for a in artifacts if a["artifact_type"] == "context_envelope"]
    assert len(envelope_arts) == 1
    env_art = envelope_arts[0]
    assert env_art["name"] == "context_envelope"
    assert env_art["artifact_metadata"]["objective"] == "Do envelope test"

    # 3. Status is recorded in runtime_manifest
    assert response["dispatch"]["runtime_manifest"]["context_envelope_status"] == "assembled"


@pytest.mark.asyncio
async def test_await_result_node_attaches_envelope_on_failure(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Envelope must be attached to result.artifacts even when worker fails."""
    worker = StubWorker(status="failure", failure_kind="worker_failure")
    await_node = build_await_result_node(
        worker,
        available_profile_names=frozenset(),
        session_factory=db_session_factory,
        context_envelope_enabled=True,
    )

    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "task-fail-1", "task_text": "Failing task"},
            "session": {
                "session_id": "sess-fail-1",
                "user_id": "u1",
                "channel": "web",
                "external_thread_id": "t1",
            },
            "dispatch": WorkerDispatch(worker_type="antigravity"),
            "route": RouteDecision(chosen_worker="antigravity"),
        }
    )

    response = await await_node(state)
    result_data = response["result"]
    assert result_data["status"] == "failure"

    envelope_arts = [
        a for a in result_data["artifacts"] if a["artifact_type"] == "context_envelope"
    ]
    assert len(envelope_arts) == 1
    assert envelope_arts[0]["artifact_metadata"]["objective"] == "Failing task"


@pytest.mark.asyncio
async def test_await_result_node_opt_out_disabled(
    db_session_factory: sessionmaker[Session],
) -> None:
    """When context_envelope_enabled=False, envelope is omitted and manifest records disabled."""
    worker = StubWorker(status="success")
    await_node = build_await_result_node(
        worker,
        available_profile_names=frozenset(),
        session_factory=db_session_factory,
        context_envelope_enabled=False,
    )

    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "task-dis-1", "task_text": "Disabled task"},
            "session": {
                "session_id": "sess-dis-1",
                "user_id": "u1",
                "channel": "web",
                "external_thread_id": "t1",
            },
            "dispatch": WorkerDispatch(worker_type="antigravity"),
            "route": RouteDecision(chosen_worker="antigravity"),
        }
    )

    response = await await_node(state)
    assert worker.last_request is not None
    assert worker.last_request.context_envelope is None

    result_data = response["result"]
    envelope_arts = [
        a for a in result_data["artifacts"] if a["artifact_type"] == "context_envelope"
    ]
    assert len(envelope_arts) == 0
    assert response["dispatch"]["runtime_manifest"]["context_envelope_status"] == "disabled"


def test_artifact_persistence_stores_metadata(
    db_session_factory: sessionmaker[Session],
) -> None:
    """_persist_artifacts_for_run must persist artifact_metadata to the DB artifacts table."""
    task_id, run_id = _seed_db_run(db_session_factory)

    envelope, status = assemble_context_envelope(
        task_id=task_id,
        session_id="s-persist",
        task_spec=TaskSpec(goal="Persist test"),
        task_text="Persist test",
        memory_context=None,
        worker_request=WorkerRequest(task_text="Persist test"),
    )
    assert status == "assembled"
    assert envelope is not None
    envelope_meta = envelope.model_dump(mode="json")
    envelope_meta["objective"] = (
        'Persist test; password=unknown-persistence-secret; {"api_key":"json-persistence-secret"}'
    )
    art_ref = ArtifactReference(
        name="context_envelope",
        uri="envelope://123",
        artifact_type="context_envelope",
        artifact_metadata=envelope_meta,
    )

    with db_session_factory() as session:
        repo = ArtifactRepository(session)
        _persist_artifacts_for_run(
            repo,
            worker_run_id=run_id,
            artifacts=[art_ref],
            review_artifact_entries=[],
        )
        session.commit()

    with db_session_factory() as session:
        persisted = session.scalars(select(Artifact).where(Artifact.run_id == run_id)).all()
        assert len(persisted) == 1
        row = persisted[0]
        assert row.artifact_type == ArtifactType.CONTEXT_ENVELOPE
        assert row.name == "context_envelope"
        assert row.artifact_metadata is not None
        assert row.artifact_metadata["objective"] == (
            'Persist test; password=[REDACTED]; {"api_key":"[REDACTED]"}'
        )
        assert "unknown-persistence-secret" not in str(row.artifact_metadata)
        assert "json-persistence-secret" not in str(row.artifact_metadata)
        assert (
            row.artifact_metadata["context_content_digest"]
            != envelope_meta["context_content_digest"]
        )
        content_digest, evidence_digest = _compute_digests(row.artifact_metadata)
        assert row.artifact_metadata["context_content_digest"] == content_digest
        assert row.artifact_metadata["evidence_digest"] == evidence_digest

    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": task_id, "task_text": "Persist test"},
            "result": WorkerResult(
                status="success",
                summary="Persistence completed successfully with durable evidence.",
                artifacts=[art_ref],
            ),
        }
    )
    artifact_index, _review_entries = _build_artifact_index(state, [art_ref])
    assert len(artifact_index) == 1
    assert artifact_index[0]["artifact_type"] == "context_envelope"
    assert "artifact_metadata" not in artifact_index[0]


@pytest.mark.asyncio
async def test_await_result_node_decomposed_dag_retains_envelope_artifacts_and_status(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Decomposed nodes assemble per-node envelopes, retain them in node outcomes,
    and set decomposed_aggregated."""
    _seed_decomposed_task_and_plan(db_session_factory, task_id="task-decomp-1")
    worker = StubWorker(status="success")
    await_node = build_await_result_node(
        worker,
        available_profile_names=frozenset(),
        session_factory=db_session_factory,
        context_envelope_enabled=True,
    )

    node1 = DecomposedTaskNode(
        node_id="step1",
        title="First step",
        task_spec=TaskSpec(goal="Do step 1", acceptance_criteria=["AC1"]),
        node_kind="inspect",
        depends_on=[],
    )
    node2 = DecomposedTaskNode(
        node_id="step2",
        title="Second step",
        task_spec=TaskSpec(goal="Do step 2", acceptance_criteria=["AC2"]),
        node_kind="implement",
        depends_on=["step1"],
    )
    decomp_plan = DecomposedTaskPlan(
        triggered=True,
        status="decomposed",
        nodes=[node1, node2],
    )

    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "task-decomp-1", "task_text": "Decomposed workflow task"},
            "session": {
                "session_id": "sess-decomp-1",
                "user_id": "u1",
                "channel": "web",
                "external_thread_id": "t1",
            },
            "dispatch": WorkerDispatch(worker_type="antigravity"),
            "route": RouteDecision(chosen_worker="antigravity"),
            "decomposed_plan": decomp_plan,
        }
    )

    response = await await_node(state)
    manifest = response["dispatch"]["runtime_manifest"]
    assert manifest is not None
    assert manifest["context_envelope_status"] == "decomposed_aggregated"

    # Verify each node outcome received its envelope artifact
    node_outcomes = response.get("node_outcomes", [])
    assert len(node_outcomes) == 2
    for node_out in node_outcomes:
        node_id = node_out["node_id"]
        artifacts = node_out["result"]["artifacts"]
        env_arts = [
            a
            for a in artifacts
            if a["artifact_type"] == "context_envelope"
            and a["name"] == f"context_envelope_{node_id}"
        ]
        assert len(env_arts) == 1
        assert env_arts[0]["artifact_metadata"]["node_id"] == node_id


@pytest.mark.asyncio
async def test_decomposed_dag_persists_envelope_in_attempts_and_replays(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Verify ExecutionPlanNodeAttempt contains envelope in result_payload and replays cleanly."""
    task_id, plan_id = _seed_decomposed_task_and_plan(db_session_factory, task_id="task-replay-1")
    worker = StubWorker(status="success")
    await_node = build_await_result_node(
        worker,
        available_profile_names=frozenset(),
        session_factory=db_session_factory,
        context_envelope_enabled=True,
    )

    decomp_plan = DecomposedTaskPlan(
        triggered=True,
        status="decomposed",
        nodes=[
            DecomposedTaskNode(
                node_id="step1",
                title="First step",
                task_spec=TaskSpec(goal="Do step 1", acceptance_criteria=["AC1"]),
                node_kind="inspect",
                depends_on=[],
            ),
            DecomposedTaskNode(
                node_id="step2",
                title="Second step",
                task_spec=TaskSpec(goal="Do step 2", acceptance_criteria=["AC2"]),
                node_kind="implement",
                depends_on=["step1"],
            ),
        ],
    )
    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": task_id, "task_text": "Decomposed replay task"},
            "session": {
                "session_id": "s1",
                "user_id": "u1",
                "channel": "web",
                "external_thread_id": "t1",
            },
            "dispatch": WorkerDispatch(worker_type="antigravity"),
            "route": RouteDecision(chosen_worker="antigravity"),
            "decomposed_plan": decomp_plan,
        }
    )

    first_resp = await await_node(state)
    manifest = first_resp["dispatch"]["runtime_manifest"]
    assert manifest["context_envelope_status"] == "decomposed_aggregated"

    # Verify attempt in DB has the envelope in result_payload
    with db_session_factory() as session:
        attempts = session.scalars(select(ExecutionPlanNodeAttempt)).all()
        step1_att = next(a for a in attempts if "step1" in (a.logical_activity_key or ""))
        assert step1_att.result_payload is not None
        art_names = [a["name"] for a in step1_att.result_payload["worker_result"]["artifacts"]]
        assert "context_envelope_step1" in art_names

    # Terminal replay: running await_node again retrieves from DB and retains envelope
    replay_resp = await await_node(state)
    replay_outcomes = replay_resp.get("node_outcomes", [])
    assert len(replay_outcomes) == 2
    for node_out in replay_outcomes:
        art_names = [a["name"] for a in node_out["result"]["artifacts"]]
        assert f"context_envelope_{node_out['node_id']}" in art_names


def _setup_smoke_git_worktree(tmp_path: Path, ws_id: str) -> tuple[Path, Path, str]:
    """Helper to setup trusted git directory and workspace with an initial commit."""
    ws_dir = tmp_path / ws_id
    ws_dir.mkdir(parents=True)
    (ws_dir / "app.py").write_text("print('hello smoke')", encoding="utf-8")
    trusted_git_dir = tmp_path / ".code-agent-git" / ws_id
    subprocess.run(["git", "init", "--bare", str(trusted_git_dir)], check=True, capture_output=True)
    subprocess.run(
        ["git", "--git-dir", str(trusted_git_dir), "--work-tree", str(ws_dir), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(trusted_git_dir),
            "--work-tree",
            str(ws_dir),
            "-c",
            "user.name=smoke",
            "-c",
            "user.email=smoke@example.com",
            "commit",
            "-m",
            "smoke commit",
        ],
        check=True,
        capture_output=True,
    )
    commit_proc = subprocess.run(
        ["git", "--git-dir", str(trusted_git_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return trusted_git_dir, ws_dir, commit_proc.stdout.strip().lower()


def _drive_smoke_terminal_outcome(
    service: TaskExecutionService,
    state: OrchestratorState,
    node_activity_key: str,
) -> None:
    """Drive the production terminal outcome path through

    TaskExecutionService._persist_execution_outcome.
    """
    with session_scope(service.session_factory) as session:
        attempt = session.scalar(
            select(ExecutionPlanNodeAttempt).where(
                ExecutionPlanNodeAttempt.logical_activity_key == node_activity_key
            )
        )
        assert attempt is not None and attempt.result_payload is not None
        node_result = WorkerResult.model_validate(attempt.result_payload["worker_result"])

    state.result = node_result
    service._persist_execution_outcome(
        task_id=state.task.task_id,
        state=state,
        started_at=utc_now(),
        finished_at=utc_now(),
    )


def _build_smoke_decomposed_state(task_id: str, ws_id: str) -> OrchestratorState:
    """Helper to build orchestrator state for decomposed node smoke test."""
    return OrchestratorState.model_validate(
        {
            "task": {"task_id": task_id, "task_text": "Temporal task"},
            "session": {
                "session_id": "s1",
                "user_id": "u1",
                "channel": "web",
                "external_thread_id": "t1",
            },
            "dispatch": WorkerDispatch(worker_type="antigravity", workspace_id=ws_id),
            "route": RouteDecision(chosen_worker="antigravity"),
            "decomposed_plan": DecomposedTaskPlan(
                triggered=True,
                status="decomposed",
                nodes=[
                    DecomposedTaskNode(
                        node_id="step1",
                        title="Do step 1",
                        task_spec=TaskSpec(goal="Do step 1", acceptance_criteria=["AC1"]),
                        node_kind="inspect",
                        depends_on=[],
                    )
                ],
            ),
        }
    )


def _assert_operator_api_envelope(
    service: TaskExecutionService, task_id: str, trusted_sha: str
) -> None:
    """Assert GET /tasks/{task_id} returns the persisted context envelope artifact."""
    app = create_app(
        task_service=service,
        auth_config=ApiAuthConfig(shared_secret="a" * 32),
    )
    with TestClient(app) as client:
        client.headers["X-Webhook-Token"] = "a" * 32
        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["latest_run"] is not None
        envelope_arts = [
            a for a in data["latest_run"]["artifacts"] if a["artifact_type"] == "context_envelope"
        ]
        assert len(envelope_arts) == 1
        meta = envelope_arts[0]["artifact_metadata"]
        assert meta["repo_facts"]["commit_sha"] == trusted_sha
        assert meta["repo_facts"]["worktree_state_digest"] == hashlib.sha256(b"clean").hexdigest()
        assert meta["context_content_digest"]


@pytest.mark.asyncio
async def test_temporal_run_decomposed_node_attaches_envelope_smoke(
    db_session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production end-to-end smoke test driving genuine TaskExecutionActivities and

    TaskExecutionService._persist_execution_outcome to verify context envelope delivery,
    trusted git resolution, production artifact persistence, and operator API projection.
    """
    monkeypatch.setattr(activities_module.activity, "heartbeat", lambda *a, **kw: None)
    task_id, ws_id = "task-smoke-e2e", "ws-smoke-e2e"
    _trusted_git_dir, _ws_dir, trusted_sha = _setup_smoke_git_worktree(tmp_path, ws_id)
    stub_worker = StubWorker()
    service = TaskExecutionService(
        session_factory=db_session_factory,
        worker=stub_worker,
        workspace_root=tmp_path,
    )
    activities = TaskExecutionActivities(service=service)

    # Seed task state in temporal repository
    state = _build_smoke_decomposed_state(task_id, ws_id)
    with session_scope(db_session_factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id, state=state.model_dump(mode="json")
        )

    _, plan_id = _seed_decomposed_task_and_plan(db_session_factory, task_id, num_nodes=1)
    node = state.decomposed_plan.nodes[0]
    _, digest = _effective_input_evidence(state, node, {})
    node_act = NodeActivityRequest(
        task_id=task_id,
        plan_id=plan_id,
        node_id="step1",
        logical_attempt=1,
        logical_activity_key=logical_activity_key(plan_id, "step1", 1),
        effective_input_digest=digest,
    )
    result_ref = await activities.run_decomposed_node(task_id, node_act.model_dump(mode="json"))
    assert result_ref["status"] == "completed"

    # 1. Assert worker received context envelope with trusted git commit sha & worktree digest
    assert stub_worker.last_request is not None
    assert stub_worker.last_request.context_envelope is not None
    assert stub_worker.last_request.context_envelope["node_id"] == "step1"
    assert stub_worker.last_request.context_envelope["repo_facts"]["commit_sha"] == trusted_sha
    assert (
        stub_worker.last_request.context_envelope["repo_facts"]["worktree_state_digest"]
        == hashlib.sha256(b"clean").hexdigest()
    )

    # 2. Drive production terminal outcome persistence with envelope artifact
    _drive_smoke_terminal_outcome(service, state, node_act.logical_activity_key)

    # 3. Assert operator API projection via GET /tasks/{task_id} returns context envelope
    _assert_operator_api_envelope(service, task_id, trusted_sha)


def test_temporal_project_decomposed_runtime_manifest_sets_status() -> None:
    """_project_decomposed_runtime_manifest explicitly sets context_envelope_status
    to decomposed_aggregated."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "task-proj-1", "task_text": "Decomposed project task"},
            "dispatch": WorkerDispatch(
                worker_type="antigravity",
                runtime_manifest={"context_envelope_status": "assembled"},
            ),
        }
    )
    _project_decomposed_runtime_manifest(state)
    assert state.dispatch.runtime_manifest is not None
    assert state.dispatch.runtime_manifest["context_envelope_status"] == "decomposed_aggregated"
    assert state.dispatch.runtime_manifest["worker"]["worker_type"] == "antigravity"


def test_temporal_project_decomposed_runtime_manifest_disabled_when_flag_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When CODE_AGENT_CONTEXT_ENVELOPE_ENABLED=false, status is disabled."""
    monkeypatch.setenv("CODE_AGENT_CONTEXT_ENVELOPE_ENABLED", "false")
    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "task-proj-2", "task_text": "Decomposed project task disabled"},
            "dispatch": WorkerDispatch(
                worker_type="antigravity",
                runtime_manifest={"context_envelope_status": "assembled"},
            ),
        }
    )
    _project_decomposed_runtime_manifest(state)
    assert state.dispatch.runtime_manifest is not None
    assert state.dispatch.runtime_manifest["context_envelope_status"] == "disabled"


def test_temporal_project_decomposed_runtime_manifest_disabled_when_no_envelopes() -> None:
    """When no envelopes exist and status is not set, status is disabled."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "task-proj-3", "task_text": "Decomposed project task no envs"},
            "dispatch": WorkerDispatch(
                worker_type="antigravity",
                runtime_manifest={},
            ),
        }
    )
    _project_decomposed_runtime_manifest(state)
    assert state.dispatch.runtime_manifest is not None
    assert state.dispatch.runtime_manifest["context_envelope_status"] == "disabled"
