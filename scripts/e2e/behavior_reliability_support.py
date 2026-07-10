"""Support utilities and runners for the M23.11 behavior reliability evaluation."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse, urlunparse
from urllib.request import url2pathname

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
DUMMY_REPO_MARKER = ".behavior-reliability-dummy"
LIVE_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"completed", "success", "failed", "cancelled", "error", "awaiting_approval"}
)


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


def write_report(
    output: str,
    base_url: str,
    mode: str,
    run_id: str,
    started_at: str,
    results: list[CaseResult],
    cleanup_errors: list[str],
    passed_all: bool,
) -> None:
    """Write the evaluator report to a caller-selected path."""
    report = {
        "run_id": run_id,
        "started_at": started_at,
        "base_url": base_url,
        "mode": mode,
        "passed": passed_all,
        "cases": [
            {
                "case_id": result.case_id,
                "passed": result.passed,
                "task_id": result.task_id,
                "seeded_memory_ids": result.seeded_memory_keys,
                "assertions": [
                    {
                        "name": assertion.name,
                        "passed": assertion.passed,
                        "message": assertion.message,
                    }
                    for assertion in result.assertions
                ],
                "timeline_summary": result.timeline_summary,
                "errors": result.errors,
            }
            for result in results
        ],
        "cleanup_errors": cleanup_errors,
    }
    output_dir = os.path.dirname(output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)


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
                val = parse_env_value(val)
                if key and key not in os.environ:
                    os.environ[key] = val


def parse_env_value(value: str) -> str:
    """Parse an env value while preserving hashes inside quoted values."""
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] in {"'", '"'}:
        quote = stripped[0]
        closing_index = stripped.rfind(quote)
        if closing_index > 0:
            suffix = stripped[closing_index + 1 :].strip()
            if not suffix or suffix.startswith("#"):
                return stripped[1:closing_index]
    return stripped.split("#", 1)[0].strip()


def remove_readonly(func: Any, path: str, _exc_info: Any) -> None:
    """Retry removal after clearing read-only attributes, including on Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def setup_dummy_repo(repo_dir: str) -> None:
    """Initialize a local dummy git repository with config and protected files."""
    resolved_repo_dir = os.path.abspath(os.path.expanduser(repo_dir))
    if os.path.exists(resolved_repo_dir):
        marker_path = os.path.join(resolved_repo_dir, DUMMY_REPO_MARKER)
        if not os.path.isfile(marker_path):
            raise ValueError(
                f"Refusing to delete unmarked existing directory: {resolved_repo_dir}. "
                f"Only evaluator-owned repositories with {DUMMY_REPO_MARKER} may be replaced."
            )
        shutil.rmtree(resolved_repo_dir, onerror=remove_readonly)

    os.makedirs(resolved_repo_dir, exist_ok=True)
    subprocess.run(["git", "init"], cwd=resolved_repo_dir, check=True, capture_output=True)

    with open(os.path.join(resolved_repo_dir, DUMMY_REPO_MARKER), "w", encoding="utf-8") as f:
        f.write("behavior-reliability-evaluator-owned\n")

    protected_files = [".env", "secrets.json", "billing.yml", ".github/workflows/deploy.yml"]
    for filename in ["README.md"] + protected_files:
        filepath = os.path.join(resolved_repo_dir, filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# Dummy {filename} content\n")

    yaml_content = "protected_paths:\n" + "\n".join(f'  - "{p}"' for p in protected_files) + "\n"
    with open(
        os.path.join(resolved_repo_dir, "code-agent.project.yaml"), "w", encoding="utf-8"
    ) as f:
        f.write(yaml_content)

    subprocess.run(
        ["git", "config", "user.name", "QA User"],
        cwd=resolved_repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "qa@example.com"],
        cwd=resolved_repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=resolved_repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=resolved_repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-M", "master"],
        cwd=resolved_repo_dir,
        check=True,
        capture_output=True,
    )


def is_evaluator_owned_repo(repo_dir: str) -> bool:
    """Return whether a path contains the evaluator ownership marker."""
    marker_path = os.path.join(os.path.abspath(os.path.expanduser(repo_dir)), DUMMY_REPO_MARKER)
    try:
        with open(marker_path, encoding="utf-8") as f:
            return f.read().strip() == "behavior-reliability-evaluator-owned"
    except OSError:
        return False


def local_file_from_uri(uri: Any) -> Path | None:
    """Resolve a local file URI emitted by the worker artifact index."""
    if not isinstance(uri, str):
        return None
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return Path(url2pathname(parsed.path))
    if parsed.scheme == "" and uri:
        return Path(uri)
    return None


def live_artifact_text(task_data: dict[str, Any], *names: str) -> str:
    """Read available local worker artifacts for execution evidence."""
    latest_run = task_data.get("latest_run") or {}
    artifacts = list(latest_run.get("artifact_index") or []) + list(
        latest_run.get("artifacts") or []
    )
    wanted = set(names)
    chunks: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("name") not in wanted:
            continue
        path = local_file_from_uri(artifact.get("uri"))
        if path is None:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def live_profile_command_was_executed(task_data: dict[str, Any]) -> bool:
    """Require command evidence independent of the prompt that requested it."""
    marker = "profile_verification_utilization"
    latest_run = task_data.get("latest_run") or {}
    for command in latest_run.get("commands_run") or []:
        if not isinstance(command, dict):
            continue
        text = command.get("command") or command.get("cmd") or ""
        if marker in text and " -p " not in text and "--prompt" not in text:
            return True
    return marker in live_artifact_text(task_data, "native-agent-stdout", "worker-stdout", "stdout")


def live_workspace_path(task_data: dict[str, Any]) -> Path | None:
    """Find the worker workspace artifact used by a live run."""
    latest_run = task_data.get("latest_run") or {}
    artifacts = list(latest_run.get("artifact_index") or []) + list(
        latest_run.get("artifacts") or []
    )
    for artifact in artifacts:
        if isinstance(artifact, dict) and artifact.get("name") == "workspace":
            path = local_file_from_uri(artifact.get("uri"))
            if path is not None:
                return path
    return None


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
        db_url = os.path.expandvars(db_url)
        parsed_db_url = urlparse(db_url)
        if parsed_db_url.scheme.startswith("sqlite"):
            db_path = parsed_db_url.path
            if db_path.startswith("/~"):
                db_path = "/" + os.path.expanduser(db_path[1:])
            else:
                db_path = os.path.expanduser(db_path)
            if parsed_db_url.netloc:
                db_url = urlunparse(parsed_db_url._replace(path=db_path))
            else:
                db_url = f"{parsed_db_url.scheme}://{db_path}"
                if parsed_db_url.query:
                    db_url += f"?{parsed_db_url.query}"
            engine_kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
            if parsed_db_url.path in {":memory:", "/:memory:"}:
                engine_kwargs["poolclass"] = StaticPool
            engine = create_engine_from_url(db_url, **engine_kwargs)
        else:
            engine = create_engine_from_url(os.path.expanduser(db_url))
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
        self._memory_snapshots: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._seeded_values: dict[tuple[str, str], dict[str, Any]] = {}

    def close(self) -> None:
        """Close the live runner's HTTP client and its connection pool."""
        self.client.close()

    def _list_memory(self, category: str) -> list[dict[str, Any]]:
        """List one memory category through the live API."""
        path = f"{self.base_url}/knowledge-base/{category}"
        params = {"limit": 200, "offset": 0}
        if category == "project":
            params["repo_url"] = self.repo_url
        resp = self.client.get(path, params=params)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            raise TypeError(f"Unexpected {category} memory response: {type(payload).__name__}")
        return [item for item in payload if isinstance(item, dict)]

    def _snapshot_memory(self, category: str, key: str) -> None:
        """Save a canonical memory before the evaluator overwrites it."""
        identity = (category, key)
        if identity in self._memory_snapshots:
            return
        repo_url = self.repo_url if category == "project" else None
        match = next(
            (
                item
                for item in self._list_memory(category)
                if item.get("memory_key") == key
                and (category != "project" or item.get("repo_url") == repo_url)
            ),
            None,
        )
        self._memory_snapshots[identity] = match

    def _current_memory(self, category: str, key: str) -> dict[str, Any] | None:
        """Read the current exact memory entry for ownership-safe cleanup."""
        repo_url = self.repo_url if category == "project" else None
        return next(
            (
                item
                for item in self._list_memory(category)
                if item.get("memory_key") == key
                and (category != "project" or item.get("repo_url") == repo_url)
            ),
            None,
        )

    def _upsert(self, category: str, payload: dict[str, Any]) -> None:
        resp = self.client.put(
            f"{self.base_url}/knowledge-base/{category}", json=_json_safe(payload)
        )
        resp.raise_for_status()

    def _seed(self, category: str, key: str, payload: dict[str, Any]) -> None:
        self._snapshot_memory(category, key)
        self._seeded_values[(category, key)] = dict(payload["value"])
        self._upsert(category, payload)

    def seed_personal(self, key: str, value: dict, **kwargs) -> None:
        payload = {"memory_key": key, "value": value}
        payload.update(kwargs)
        self._seed("personal", key, payload)

    def seed_project(self, key: str, value: dict, **kwargs) -> None:
        payload = {"repo_url": self.repo_url, "memory_key": key, "value": value}
        payload.update(kwargs)
        self._seed("project", key, payload)

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

    def restore_memories(self, keys: list[str]) -> list[str]:
        """Restore overwritten memories without deleting concurrent user data."""
        cleanup_errors: list[str] = []
        identities = [identity for identity in self._memory_snapshots if identity[1] in keys]
        for category, key in identities:
            original = self._memory_snapshots[(category, key)]
            try:
                current = self._current_memory(category, key)
                current_value = current.get("value") if current else None
                seeded_value = self._seeded_values.get((category, key))
                evaluator_owned = isinstance(current_value, dict) and (
                    current_value.get("eval_run_id") == self.run_id or current_value == seeded_value
                )
                if original is None:
                    if current is None:
                        continue
                    if not evaluator_owned:
                        cleanup_errors.append(f"Skipped deleting modified {category} memory {key}.")
                        continue
                    if category == "project":
                        self.delete_project(key)
                    else:
                        self.delete_personal(key)
                    continue
                if current is None:
                    cleanup_errors.append(f"Skipped restoring deleted {category} memory {key}.")
                    continue
                if not evaluator_owned:
                    cleanup_errors.append(f"Skipped restoring modified {category} memory {key}.")
                    continue
                restore_payload = {
                    field: original[field]
                    for field in (
                        "memory_key",
                        "value",
                        "source",
                        "confidence",
                        "scope",
                        "last_verified_at",
                        "requires_verification",
                    )
                    if field in original
                }
                if category == "project":
                    restore_payload["repo_url"] = self.repo_url
                self._upsert(category, restore_payload)
            except Exception as exc:
                cleanup_errors.append(f"Failed restoring {category} memory {key}: {exc}")
        return cleanup_errors

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
                try:
                    t_resp = await client.get(f"{self.base_url}/tasks/{task_id}")
                    t_resp.raise_for_status()
                    task_data = t_resp.json()
                    if task_data.get("status") in LIVE_TERMINAL_STATUSES:
                        break
                except httpx.HTTPError:
                    continue
            else:
                raise RuntimeError(f"Task {task_id} timed out.")
            return {"task_data": task_data, "task_id": task_id}
