"""Independent verification audits its own invocation on an already dirty workspace."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.state import OrchestratorState
from orchestrator.verification import run_independent_verifier
from workers.base import Worker, WorkerResult
from workers.gemini_cli_worker_native import GeminiCliWorkerNativeMixin
from workers.native_agent_runner import NativeAgentRunRequest, run_native_agent


class NativeFixtureWorker(Worker):
    def __init__(self, repo, *, auth_error=False, mutate=False):
        self.repo = repo
        self.auth_error = auth_error
        self.mutate = mutate
        self.calls = 0
        self.native_result = None

    async def run(self, request, *, system_prompt=None):
        assert request.read_only
        self.calls += 1

        def execute(**_kwargs):
            if self.mutate:
                (self.repo / "edit.txt").write_text("verifier mutation\n")
            return (
                subprocess.CompletedProcess(
                    ["fixture"],
                    1 if self.auth_error else 0,
                    "" if self.auth_error else '{"status":"passed","summary":"Verified"}',
                    "Error: authentication required" if self.auth_error else "",
                ),
                None,
            )

        with patch("workers.native_agent_runner._execute_native_agent_subprocess", execute):
            result = run_native_agent(
                NativeAgentRunRequest(
                    command=["fixture"],
                    prompt="Verify the existing edit",
                    repo_path=self.repo,
                    workspace_path=self.repo.parent,
                    read_only_workspace=True,
                )
            )
        self.native_result = result
        kind = GeminiCliWorkerNativeMixin()._native_failure_kind(result)
        return WorkerResult(
            status=result.status,
            summary=result.summary,
            failure_kind=kind,
            json_payload=result.json_payload,
            files_changed=result.files_changed,
            artifacts=result.artifacts,
        )


@pytest.fixture
def dirty_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in [
        ["init"],
        ["config", "user.name", "Fixture"],
        ["config", "user.email", "fixture@example.invalid"],
    ]:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True
    )
    (repo / "edit.txt").write_text("executor change\n")
    return repo


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [False, True])
async def test_verifier_auth_fallback_distinguishes_inherited_edits_from_mutation(
    dirty_repo, mutate
):
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Implement a fix"},
            "dispatch": {"worker_type": "codex", "workspace_id": "fixture"},
            "result": {
                "status": "success",
                "files_changed": ["edit.txt"],
                "summary": "Implemented",
            },
        }
    )
    first = NativeFixtureWorker(dirty_repo, auth_error=True, mutate=mutate)
    fallback = NativeFixtureWorker(dirty_repo)
    status, _, reason = await run_independent_verifier(
        state, worker_factory={"antigravity": first, "codex": fallback}
    )
    assert first.calls == 1
    if mutate:
        assert status == "warning"
        assert reason == "infra_verifier_unavailable"
        assert fallback.calls == 0
        assert "READ_ONLY_VIOLATION" in first.native_result.summary
    else:
        assert status == "passed", (reason, first.native_result, fallback.native_result)
        assert reason is None
        assert fallback.calls == 1
        assert first.native_result.files_changed == []
        assert fallback.native_result.files_changed == []
        assert first.native_result.artifacts
        assert (dirty_repo / "edit.txt").read_text() == "executor change\n"
