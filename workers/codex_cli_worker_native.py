"""Codex CLI worker backed by the shared CLI runtime."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from apps.observability import (
    set_span_status_from_outcome,
)
from db.enums import WorkerRuntimeMode
from sandbox import (
    DockerSandboxContainer,
    WorkspaceHandle,
)
from sandbox.policy import is_in_container
from sandbox.redact import SecretRedactor
from tools import (
    ToolPermissionLevel,
)
from workers.adapter_utils import build_failure_summary, format_native_run_summary
from workers.base import (
    ArtifactReference,
    FailureKind,
    WorkerCommand,
    WorkerRequest,
    WorkerResult,
)
from workers.cli_adapter_utils import (
    build_worker_result,
    resolve_runtime_mode,
    runtime_mode_not_supported_result,
)
from workers.cli_runtime import (
    CliRuntimeExecutionResult,
    CliRuntimeSettings,
    ShellSessionProtocol,
)
from workers.failure_taxonomy import classify_failure_kind
from workers.native_agent_models import NativeAgentRunResult
from workers.native_agent_runner import (
    NativeAgentRunRequest,
    run_native_agent,
)
from workers.prompt import build_system_prompt
from workers.review import ReviewResult

logger = logging.getLogger(__name__)

DEFAULT_CODEX_NATIVE_SANDBOX_MODE = "workspace-write"


@dataclass
class _RuntimeSetup:
    """Container/session/runtime context prepared before entering the CLI loop."""

    container: DockerSandboxContainer
    session: ShellSessionProtocol
    runtime_settings: CliRuntimeSettings
    granted_permission: ToolPermissionLevel
    expects_changed_files: bool
    fallback_command_template: str | None
    system_prompt: str


@dataclass
class _RuntimeExecutionPhase:
    """Outputs captured from runtime execution and post-processing phases."""

    execution: CliRuntimeExecutionResult
    files_changed: list[str]
    lint_format_result: dict[str, object] | None
    lint_format_artifacts: list[ArtifactReference]
    review_result: ReviewResult | None


class ShellSessionFactory(Protocol):
    """Factory for opening a persistent shell session in a running container."""

    def __call__(
        self,
        container: DockerSandboxContainer,
        *,
        secrets: dict[str, str] | None = None,
    ) -> ShellSessionProtocol:
        """Return a ready-to-use shell session."""


def _slugify(value: str) -> str:
    """Create a filesystem-safe slug for sandbox bookkeeping."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "task"


def _workspace_task_id(request: WorkerRequest) -> str:
    """Build a readable workspace task identifier from the worker request."""
    if request.task_id:
        return request.task_id
    source = request.session_id or request.task_text
    return f"codex-cli-{_slugify(source)}"


def _workspace_artifacts(workspace: WorkspaceHandle) -> list[ArtifactReference]:
    """Build the default artifact references for a retained workspace."""
    return [
        ArtifactReference(
            name="workspace",
            uri=workspace.workspace_path.as_uri(),
            artifact_type="workspace",
        )
    ]


def _apply_cleanup_outcome(result: WorkerResult, *, workspace_deleted: bool) -> WorkerResult:
    """Keep the reported result aligned with the final workspace state."""
    if not workspace_deleted:
        return result

    summary = (
        f"{result.summary.rstrip('.')} Workspace cleaned up per policy."
        if result.summary
        else "CodexCliWorker cleaned up the workspace per policy."
    )
    return result.model_copy(
        update={
            "summary": summary,
            "artifacts": [],
            "next_action_hint": None,
        }
    )


def _next_action_hint(execution: CliRuntimeExecutionResult) -> str:
    """Return the best follow-up hint for a retained workspace."""
    if execution.stop_reason == "permission_required":
        return "request_higher_permission"
    if execution.stop_reason in {
        "max_iterations",
        "worker_timeout",
        "budget_exceeded",
        "stalled_in_inspection",
        "exploration_exhausted",
        "no_progress_before_budget",
    }:
        return "increase_budget_or_reduce_scope"
    if execution.stop_reason == "context_window":
        return "reduce_context_or_scope"
    if execution.stop_reason == "adapter_error":
        return "inspect_worker_configuration"
    return "inspect_workspace_artifacts"


def _worker_result_from_execution(
    workspace: WorkspaceHandle,
    execution: CliRuntimeExecutionResult,
    *,
    files_changed: list[str],
    post_run_lint_format: dict[str, object] | None = None,
    review_result: ReviewResult | None = None,
    diff_text: str | None = None,
    artifacts: list[ArtifactReference] | None = None,
) -> WorkerResult:
    """Map the shared CLI runtime output into the worker contract."""
    requested_permission = (
        execution.permission_decision.required_permission.value
        if execution.permission_decision is not None
        else None
    )
    budget_usage = execution.budget_ledger.model_dump(mode="json")
    if post_run_lint_format is not None:
        budget_usage["post_run_lint_format"] = post_run_lint_format
    return build_worker_result(
        execution=execution,
        files_changed=files_changed,
        requested_permission=requested_permission,
        post_run_lint_format=post_run_lint_format,
        review_result=review_result,
        diff_text=diff_text,
        artifacts=[*_workspace_artifacts(workspace), *(artifacts or [])],
        next_action_hint=_next_action_hint(execution),
        workspace_id=workspace.workspace_id,
    )


def _workspace_error_result(
    *,
    request: WorkerRequest,
    workspace: WorkspaceHandle,
    workspace_task_id: str,
    exc: Exception,
    summary_prefix: str,
    next_action_hint: str,
) -> WorkerResult:
    """Log a workspace-scoped failure and map it into the worker contract."""
    logger.exception(
        "Codex CLI worker failed inside a provisioned workspace",
        extra={
            "session_id": request.session_id,
            "workspace_id": workspace.workspace_id,
            "workspace_task_id": workspace_task_id,
        },
    )
    return WorkerResult(
        status="error",
        summary=f"{summary_prefix}: {exc}",
        failure_kind="sandbox_infra",
        artifacts=_workspace_artifacts(workspace),
        next_action_hint=next_action_hint,
    )


from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    pass


class CodexCliWorkerNativeMixin:
    def _resolve_runtime_mode(self, request: WorkerRequest) -> WorkerRuntimeMode:
        """Resolve effective runtime mode from request override or worker defaults."""
        return resolve_runtime_mode(request, self.default_runtime_mode)  # type: ignore[attr-defined]

    def _runtime_mode_not_supported_result(self, runtime_mode: WorkerRuntimeMode) -> WorkerResult:
        """Return a structured failure for unsupported execution runtime modes."""
        return runtime_mode_not_supported_result(
            "CodexCliWorker", runtime_mode, ["native_agent", "tool_loop"]
        )

    def _build_native_command(
        self,
        *,
        workspace: WorkspaceHandle,
        request: WorkerRequest,
        final_message_path: Path,
        runtime_mode: WorkerRuntimeMode,
        output_schema_path: Path | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        """Build a one-shot `codex exec` command for native-agent mode."""
        executable = getattr(self.runtime_adapter, "executable", "codex")  # type: ignore[attr-defined]
        model = getattr(self.runtime_adapter, "model", None)  # type: ignore[attr-defined]
        profile = getattr(self.runtime_adapter, "profile", None)  # type: ignore[attr-defined]
        read_only_requested = request.read_only or bool(request.constraints.get("read_only"))

        # Sandbox selection policy (T-172)
        in_container = is_in_container()
        repo_approved = False
        repo_url = request.repo_url or ""
        for pattern in self.trusted_repo_patterns:  # type: ignore[attr-defined]
            if pattern.search(repo_url):
                repo_approved = True
                break

        if read_only_requested:
            sandbox_mode = "read-only"
        elif in_container and repo_approved:
            sandbox_mode = "danger-full-access"
        else:
            sandbox_mode = self.native_sandbox_mode or DEFAULT_CODEX_NATIVE_SANDBOX_MODE  # type: ignore[attr-defined]

        logger.info(
            "Selected Codex native sandbox mode: %s",
            sandbox_mode,
            extra={
                "session_id": request.session_id,
                "in_container": in_container,
                "repo_approved": repo_approved,
                "read_only_requested": read_only_requested,
            },
        )

        sandbox_metadata = {
            "sandbox_mode": sandbox_mode,
            "in_container": in_container,
            "repo_approved": repo_approved,
            "read_only_requested": read_only_requested,
        }

        command = [
            executable,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            sandbox_mode,
            "--color",
            "never",
            "--output-last-message",
            str(final_message_path),
            "--ephemeral",
            "-C",
            str(workspace.repo_path),
        ]
        if model:
            command.extend(["--model", str(model)])
        if profile:
            command.extend(["--profile", str(profile)])
        if output_schema_path:
            command.extend(["--output-schema", str(output_schema_path)])
        if self.native_event_capture_enabled and runtime_mode == WorkerRuntimeMode.NATIVE_AGENT:  # type: ignore[attr-defined]
            command.append("--json")
        command.append("-")
        return command, sandbox_metadata

    def _build_native_prompt(self, *, system_prompt: str, request: WorkerRequest) -> str:
        """Build the native-agent prompt packet for one-shot Codex execution."""
        task_text = request.task_text.strip()
        delivery_mode = (request.task_spec or {}).get("delivery_mode", "workspace")
        is_read_only = delivery_mode == "summary"
        output_instructions = (
            "Return a comprehensive summary of your findings and any recommendations."
            if is_read_only
            else (
                "Return a concise final summary of what changed, verification performed, "
                "and any remaining blocker."
            )
        )
        if request.response_schema:
            import json as json_lib

            schema_json = json_lib.dumps(request.response_schema, indent=2)
            output_instructions = (
                "Return exactly one JSON object that strictly matches this JSON schema:\n"
                f"{schema_json}\n"
                "Do not include markdown fences, explanatory prose, or extra keys."
            )

        sections = [
            system_prompt.strip(),
            "## Native Execution Task",
            task_text,
            "## Output",
            output_instructions,
        ]
        return "\n\n".join(section for section in sections if section.strip())

    def _native_next_action_hint(self, native_result: NativeAgentRunResult) -> str:
        """Map native run outcomes to follow-up hints."""
        if native_result.timed_out:
            return "increase_budget_or_reduce_scope"
        if native_result.status == "error":
            return "inspect_worker_configuration"
        return "inspect_workspace_artifacts"

    def _native_failure_kind(self, native_result: NativeAgentRunResult) -> FailureKind | None:
        """Classify failure kind for native-agent outcomes."""
        if native_result.status == "success":
            return None
        if native_result.timed_out:
            return "timeout"
        summary = build_failure_summary(
            summary=format_native_run_summary(native_result),
            final_message=native_result.final_message,
        )
        return classify_failure_kind(
            status=native_result.status,
            summary=summary,
            final_message=native_result.final_message,
            commands_run=[
                WorkerCommand(
                    command=native_result.command,
                    exit_code=native_result.exit_code,
                    duration_seconds=native_result.duration_seconds,
                )
            ],
        )

    def _prepare_native_agent_run_request(
        self,
        request: WorkerRequest,
        workspace: WorkspaceHandle,
        runtime_settings: CliRuntimeSettings,
        runtime_mode: WorkerRuntimeMode,
        system_prompt_override: str | None,
    ) -> tuple[NativeAgentRunRequest, dict[str, Any]]:
        """Build the run request and setup paths for native execution."""
        system_prompt = (
            system_prompt_override
            if system_prompt_override is not None
            else build_system_prompt(
                request,
                workspace.repo_path,
                tool_registry=self.tool_registry,  # type: ignore[attr-defined]
            )
        )
        final_message_path = workspace.workspace_path / ".code-agent" / "native-final-message.json"
        final_message_path.parent.mkdir(parents=True, exist_ok=True)
        events_path = (
            workspace.workspace_path / ".code-agent" / "native-events.jsonl"
            if self.native_event_capture_enabled  # type: ignore[attr-defined]
            else None
        )
        output_schema_path = (
            workspace.workspace_path / ".code-agent" / "native-response.schema.json"
            if request.response_schema
            else None
        )
        if output_schema_path:
            output_schema_path.write_text(
                json.dumps(request.response_schema, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        command, sandbox_metadata = self._build_native_command(
            workspace=workspace,
            request=request,
            final_message_path=final_message_path,
            runtime_mode=runtime_mode,
            output_schema_path=output_schema_path,
        )
        run_request = NativeAgentRunRequest(
            command=command,
            prompt=self._build_native_prompt(system_prompt=system_prompt, request=request),
            repo_path=workspace.repo_path,
            workspace_path=workspace.workspace_path,
            timeout_seconds=runtime_settings.worker_timeout_seconds,
            diff_timeout_seconds=runtime_settings.command_timeout_seconds,
            changed_files_timeout_seconds=runtime_settings.command_timeout_seconds,
            env=getattr(self.runtime_adapter, "env", None),  # type: ignore[attr-defined]
            final_message_path=final_message_path,
            events_path=events_path,
            collect_diff=True,
            collect_changed_files=True,
            task_id=request.task_id,
            session_id=request.session_id,
            redactor=SecretRedactor(list((request.secrets or {}).values())),
            response_format=request.response_format,
            response_schema=request.response_schema,
        )
        return run_request, sandbox_metadata

    def _execute_native_runtime(
        self,
        request: WorkerRequest,
        *,
        workspace: WorkspaceHandle,
        runtime_settings: CliRuntimeSettings,
        runtime_mode: WorkerRuntimeMode,
        system_prompt_override: str | None,
        cancel_token: Callable[[], bool] | None,
    ) -> WorkerResult:
        """Execute one native-agent `codex exec` run and map it into WorkerResult."""
        if cancel_token and cancel_token():
            return WorkerResult(
                status="error",
                summary="Codex native-agent run was cancelled before execution.",
                failure_kind="timeout",
                artifacts=_workspace_artifacts(workspace),
                next_action_hint="inspect_workspace_artifacts",
            )

        run_request, sandbox_metadata = self._prepare_native_agent_run_request(
            request, workspace, runtime_settings, runtime_mode, system_prompt_override
        )
        native_result = run_native_agent(run_request)

        summary = build_failure_summary(
            summary=format_native_run_summary(native_result),
            final_message=native_result.final_message,
        )
        result = WorkerResult(
            status=native_result.status,
            summary=summary,
            failure_kind=self._native_failure_kind(native_result),
            budget_usage={
                "runtime_mode": runtime_mode.value,
                "native_agent": {
                    "duration_seconds": native_result.duration_seconds,
                    "exit_code": native_result.exit_code,
                    "timed_out": native_result.timed_out,
                    "event_capture_enabled": self.native_event_capture_enabled,  # type: ignore[attr-defined]
                    **sandbox_metadata,
                },
            },
            commands_run=[
                WorkerCommand(
                    command=native_result.command,
                    exit_code=native_result.exit_code,
                    duration_seconds=native_result.duration_seconds,
                )
            ],
            files_changed=native_result.files_changed,
            artifacts=[*_workspace_artifacts(workspace), *native_result.artifacts],
            diff_text=native_result.diff_text,
            json_payload=native_result.json_payload,
            friction_reports=native_result.friction_reports,
            next_action_hint=self._native_next_action_hint(native_result),
            stdout=native_result.stdout,
            stderr=native_result.stderr,
        )
        if cancel_token and cancel_token():
            result.status = "error"
            result.summary = "Codex native-agent run was cancelled by the orchestrator timeout."
            result.failure_kind = "timeout"
            result.next_action_hint = "inspect_workspace_artifacts"

        set_span_status_from_outcome(result.status, result.summary)
        return result
