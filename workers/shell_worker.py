"""Shell worker for deterministic command execution."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from sandbox import (
    DockerSandboxContainerManager,
    DockerSandboxContainerRequest,
    WorkspaceCleanupPolicy,
    WorkspaceManager,
    WorkspaceRequest,
)
from sandbox.workspace import default_workspace_root
from workers.async_runner import run_sync_with_cancellable_executor
from workers.base import FailureKind, Worker, WorkerCommand, WorkerRequest, WorkerResult
from workers.native_agent_runner import (
    NativeAgentRunRequest,
    run_native_agent,
)

logger = logging.getLogger(__name__)


class ShellWorker(Worker):
    """Executes deterministic shell commands in a sandbox."""

    def __init__(
        self,
        *,
        workspace_manager: WorkspaceManager | None = None,
        container_manager: DockerSandboxContainerManager | None = None,
        workspace_root: str | Path | None = None,
        cleanup_policy: WorkspaceCleanupPolicy | None = None,
    ) -> None:
        self.cleanup_policy = cleanup_policy or WorkspaceCleanupPolicy(
            delete_on_success=True,
            retain_on_failure=True,
        )
        self.workspace_manager = workspace_manager or WorkspaceManager(
            workspace_root or default_workspace_root(),
            cleanup_policy=self.cleanup_policy,
        )
        self.container_manager = container_manager or DockerSandboxContainerManager()

    async def run(
        self, request: WorkerRequest, *, system_prompt: str | None = None
    ) -> WorkerResult:
        """Provision a workspace and run the shell script."""
        logger.info(
            "ShellWorker starting execution",
            extra={
                "session_id": request.session_id,
                "command_length": len(request.task_text),
            },
        )

        if not request.repo_url:
            return WorkerResult(
                status="error",
                summary="ShellWorker requires a repo_url to provision a workspace.",
                failure_kind="unknown",
            )

        def _run_sync(cancel_requested: Callable[[], bool]) -> WorkerResult:
            workspace = self.workspace_manager.create_workspace(
                WorkspaceRequest(
                    task_id=f"shell-{request.session_id or 'run'}",
                    repo_url=request.repo_url,  # type: ignore[arg-type]
                    branch=request.branch,
                    cleanup_policy=self.cleanup_policy,
                )
            )

            try:
                container = self.container_manager.start(
                    DockerSandboxContainerRequest(
                        workspace=workspace,
                        environment=request.secrets,
                    )
                )

                try:
                    setup_commands: list[WorkerCommand] = []
                    # T-174: If a diff is provided (from a previous run), apply it before verifying.
                    diff_text = request.constraints.get("apply_diff_text")
                    if diff_text:
                        logger.info(
                            "Applying diff to workspace before verification",
                            extra={"session_id": request.session_id},
                        )
                        # We use 'git apply' inside the container via a one-shot native agent run
                        apply_result = run_native_agent(
                            NativeAgentRunRequest(
                                command=[
                                    "docker",
                                    "exec",
                                    "-i",
                                    container.container_name,
                                    "git",
                                    "apply",
                                    "-",
                                ],
                                prompt=diff_text,
                                repo_path=workspace.repo_path,
                                workspace_path=workspace.workspace_path,
                                timeout_seconds=30,
                            )
                        )
                        setup_commands.append(
                            WorkerCommand(
                                command=apply_result.command,
                                exit_code=apply_result.exit_code,
                                duration_seconds=apply_result.duration_seconds,
                            )
                        )
                        if apply_result.status != "success":
                            return WorkerResult(
                                status="error",
                                summary=(
                                    f"Failed to apply changes for verification: "
                                    f"{apply_result.summary}"
                                ),
                                failure_kind="sandbox_infra",
                                commands_run=setup_commands,
                            )

                    # In SHELL mode, we treat task_text as the script content.
                    native_result = run_native_agent(
                        NativeAgentRunRequest(
                            command=[
                                "docker",
                                "exec",
                                "-i",
                                container.container_name,
                                "/bin/sh",
                                "-e",
                            ],
                            prompt=request.task_text,
                            repo_path=workspace.repo_path,
                            workspace_path=workspace.workspace_path,
                            timeout_seconds=request.budget.get("worker_timeout_seconds", 300),
                            env=request.secrets,
                        )
                    )

                    status = native_result.status
                    summary = native_result.summary
                    failure_kind: FailureKind | None = None
                    if native_result.timed_out:
                        failure_kind = "timeout"
                    elif status != "success":
                        failure_kind = "unknown"

                    return WorkerResult(
                        status=status,
                        summary=summary,
                        failure_kind=failure_kind,
                        commands_run=[
                            *setup_commands,
                            WorkerCommand(
                                command=native_result.command,
                                exit_code=native_result.exit_code,
                                duration_seconds=native_result.duration_seconds,
                            ),
                        ],
                        files_changed=native_result.files_changed,
                        artifacts=native_result.artifacts,
                        diff_text=native_result.diff_text,
                    )
                finally:
                    self.container_manager.stop(container)
            finally:
                # Cleanup is handled by WorkspaceManager based on policy
                pass

        return await run_sync_with_cancellable_executor(_run_sync)
