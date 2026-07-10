"""Support utilities and runners for the M23.11 behavior reliability evaluation."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.pool import StaticPool

from db.base import Base
from orchestrator import OrchestratorState, build_orchestrator_graph
from orchestrator.checkpoints import create_in_memory_checkpointer
from repositories import (
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import (
    Worker,
    WorkerRequest,
    WorkerResult,
)
from workers.facade import WorkerFacade
from workers.prompt_memory import build_memory_context_section

API_SHARED_SECRET_HEADER = "X-Webhook-Token"
QA_REPO_KEY = "qa-dummy"


def _json_safe(value: Any) -> Any:
    """Convert values used in API payloads to JSON-compatible primitives."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


@dataclass
class AssertionResult:
    name: str
    passed: bool
    message: str = ""


@dataclass
class CaseResult:
    case_id: str
    passed: bool = False
    task_id: str | None = None
    seeded_memory_keys: list[str] = field(default_factory=list)
    assertions: list[AssertionResult] = field(default_factory=list)
    timeline_summary: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def load_dotenv(env_path: str = ".env") -> None:
    """Load dot-env configuration variables into os.environ."""
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, sep, val = line.partition("=")
            if sep:
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
                    val = val[1:-1]
                else:
                    val = val.split("#", 1)[0].strip()
                if key and key not in os.environ:
                    os.environ[key] = val


def setup_dummy_repo(repo_dir: str) -> None:
    """Initialize a local dummy git repository with config and protected files."""
    shutil.rmtree(repo_dir, ignore_errors=True)
    os.makedirs(repo_dir, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)

    protected_files = [".env", "secrets.json", "billing.yml", ".github/workflows/deploy.yml"]
    for filename in ["README.md"] + protected_files:
        filepath = os.path.join(repo_dir, filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# Dummy {filename} content\n")

    yaml_content = "protected_paths:\n" + "\n".join(f'  - "{p}"' for p in protected_files) + "\n"
    with open(os.path.join(repo_dir, "code-agent.project.yaml"), "w", encoding="utf-8") as f:
        f.write(yaml_content)

    subprocess.run(
        ["git", "config", "user.name", "QA User"], cwd=repo_dir, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "qa@example.com"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True
    )
    subprocess.run(["git", "branch", "-M", "master"], cwd=repo_dir, check=True, capture_output=True)


class FakeBehaviorWorker(Worker):
    """Fake worker to capture graph requests and simulate specific task outcomes."""

    def __init__(self) -> None:
        self.captured_requests: list[WorkerRequest] = []
        self.captured_prompts: list[str] = []
        self.simulated_result: WorkerResult | None = None

    async def run(
        self, request: WorkerRequest, *, system_prompt: str | None = None
    ) -> WorkerResult:
        self.captured_requests.append(request)
        prompt = build_memory_context_section(request)
        self.captured_prompts.append(prompt)
        if self.simulated_result is not None:
            return self.simulated_result
        return WorkerResult(
            status="success",
            summary="Fake worker run completed successfully.",
            commands_run=[],
            files_changed=[],
        )


class ContractRunner:
    """In-process orchestrator execution runner for contract mode validation."""

    def __init__(self, run_id: str, repo_url: str) -> None:
        self.run_id = run_id
        self.repo_url = repo_url
        # Do not load the repository's .env here: contract mode must not
        # accidentally write to the developer's live database. Explicit
        # DATABASE_URL values supplied by callers remain supported.
        db_url = os.environ.get("DATABASE_URL") or "sqlite:///:memory:"
        db_url = os.path.expandvars(os.path.expanduser(db_url))
        if db_url.startswith("sqlite"):
            engine_kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
            if db_url.endswith(":memory:"):
                engine_kwargs["poolclass"] = StaticPool
            engine = create_engine_from_url(db_url, **engine_kwargs)
        else:
            engine = create_engine_from_url(db_url)
        self.session_factory = create_session_factory(engine)
        Base.metadata.create_all(bind=engine)
        self.worker = FakeBehaviorWorker()
        self.graph = build_orchestrator_graph(
            worker=WorkerFacade(codex_worker=self.worker, antigravity_worker=self.worker),
            session_factory=self.session_factory,
            checkpointer=create_in_memory_checkpointer(),
        )

    def seed_personal(self, key: str, value: dict, **kwargs) -> None:
        with session_scope(self.session_factory) as session:
            PersonalMemoryRepository(session).upsert(memory_key=key, value=value, **kwargs)

    def seed_project(self, key: str, value: dict, **kwargs) -> None:
        with session_scope(self.session_factory) as session:
            ProjectMemoryRepository(session).upsert(
                repo_url=self.repo_url, memory_key=key, value=value, **kwargs
            )

    def delete_personal(self, key: str) -> None:
        with session_scope(self.session_factory) as session:
            PersonalMemoryRepository(session).delete(memory_key=key)

    def delete_project(self, key: str) -> None:
        with session_scope(self.session_factory) as session:
            ProjectMemoryRepository(session).delete(repo_url=self.repo_url, memory_key=key)

    async def execute_task(
        self, task_text: str, constraints: dict, simulated_result: WorkerResult | None
    ) -> dict[str, Any]:
        self.worker.simulated_result = simulated_result
        thread_id = f"contract-{self.run_id}-{uuid.uuid4().hex[:8]}"
        raw_state = await self.graph.ainvoke(
            {
                "task": {
                    "task_text": task_text,
                    "repo_url": self.repo_url,
                    "branch": "master",
                    "worker_override": "codex",
                    "constraints": constraints,
                    "budget": {"max_iterations": 1},
                }
            },
            config={"configurable": {"thread_id": thread_id}},
        )
        if "__interrupt__" in raw_state:
            raw_state = {k: v for k, v in raw_state.items() if k != "__interrupt__"}
        state = OrchestratorState.model_validate(raw_state)
        return {
            "state": state,
            "requests": list(self.worker.captured_requests),
            "prompts": list(self.worker.captured_prompts),
        }


class LiveRunner:
    """REST API execution runner for live E2E mode validation."""

    def __init__(
        self,
        run_id: str,
        base_url: str,
        repo_url: str,
        secret: str,
        timeout_seconds: int = 180,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self.run_id = run_id
        self.base_url = base_url.rstrip("/")
        self.repo_url = repo_url
        self.secret = secret
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.client = httpx.Client(headers={API_SHARED_SECRET_HEADER: secret})

    def seed_personal(self, key: str, value: dict, **kwargs) -> None:
        payload = {"memory_key": key, "value": value}
        payload.update(kwargs)
        resp = self.client.put(f"{self.base_url}/knowledge-base/personal", json=_json_safe(payload))
        resp.raise_for_status()

    def seed_project(self, key: str, value: dict, **kwargs) -> None:
        payload = {"repo_url": self.repo_url, "memory_key": key, "value": value}
        payload.update(kwargs)
        resp = self.client.put(f"{self.base_url}/knowledge-base/project", json=_json_safe(payload))
        resp.raise_for_status()

    def delete_personal(self, key: str) -> None:
        resp = self.client.delete(
            f"{self.base_url}/knowledge-base/personal", params={"memory_key": key}
        )
        if resp.status_code != 404:
            resp.raise_for_status()

    def delete_project(self, key: str) -> None:
        resp = self.client.delete(
            f"{self.base_url}/knowledge-base/project",
            params={"repo_url": self.repo_url, "memory_key": key},
        )
        if resp.status_code != 404:
            resp.raise_for_status()

    async def execute_task(
        self, task_text: str, constraints: dict, simulated_result: WorkerResult | None
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(headers={API_SHARED_SECRET_HEADER: self.secret}) as client:
            payload = {
                "task_text": task_text,
                "repo_key": QA_REPO_KEY,
                "branch": "master",
                "source": f"eval-{self.run_id}",
                "worker_override": os.environ.get("CODE_AGENT_WORKER_OVERRIDE", "antigravity"),
                "constraints": constraints,
            }
            resp = await client.post(f"{self.base_url}/webhook", json=payload)
            resp.raise_for_status()
            task_id = resp.json()["task_id"]

            deadline = asyncio.get_running_loop().time() + self.timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(self.poll_interval_seconds)
                t_resp = await client.get(f"{self.base_url}/tasks/{task_id}")
                t_resp.raise_for_status()
                task_data = t_resp.json()
                if task_data.get("status") in [
                    "completed",
                    "success",
                    "failed",
                    "cancelled",
                    "error",
                    "awaiting_approval",
                ]:
                    break
            else:
                raise RuntimeError(f"Task {task_id} timed out.")
            return {"task_data": task_data, "task_id": task_id}
