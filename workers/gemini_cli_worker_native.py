"""Gemini CLI worker backed by the shared CLI runtime."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from apps.observability import (
    set_span_status_from_outcome,
)
from db.enums import WorkerRuntimeMode
from sandbox import (
    DockerSandboxContainer,
    WorkspaceHandle,
)
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
from workers.cli_adapter_utils import build_worker_result
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

DEFAULT_GEMINI_NATIVE_SANDBOX_ENABLED = True
DEFAULT_GEMINI_NATIVE_APPROVAL_MODE = "default"


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
    return f"gemini-cli-{_slugify(source)}"


def _workspace_artifacts(workspace: WorkspaceHandle) -> list[ArtifactReference]:
    """Build the default artifact references for a retained workspace."""
    return [
        ArtifactReference(
            name="workspace",
            uri=workspace.workspace_path.as_uri(),
            artifact_type="workspace",
        )
    ]


def _prepare_workspace_gemini_home(
    *,
    workspace_path: Path,
    source_gemini_home: Path | None = None,
) -> None:
    """Best-effort sync of Gemini auth/config into isolated workspace HOME."""
    candidates: list[Path] = []
    if source_gemini_home is not None:
        candidates.append(source_gemini_home)
    env_gemini_home = os.environ.get("GEMINI_HOME")
    if env_gemini_home:
        candidates.append(Path(env_gemini_home))
    try:
        candidates.append(Path.home() / ".gemini")
    except Exception:
        pass
    candidates.append(Path("/root/.gemini"))

    resolved_source: Path | None = None
    for candidate in candidates:
        expanded = candidate.expanduser()
        try:
            if expanded.exists() and expanded.is_dir():
                resolved_source = expanded
                break
        except OSError:
            continue

    if resolved_source is None:
        return

    agent_home = workspace_path / ".agent_home"
    agent_home.mkdir(parents=True, exist_ok=True)
    target = agent_home / ".gemini"
    target_settings = target / "settings.json"
    if target.exists() or target.is_symlink():
        # Repair stale or partial workspace state: if the auth settings file is
        # missing, refresh the target from the resolved source.
        if target_settings.exists():
            try:
                if not target.is_symlink() or os.readlink(target) == str(resolved_source):
                    return
            except OSError:
                pass
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
        except OSError:
            logger.warning(
                "Failed to reset stale workspace Gemini home before sync",
                extra={"target": str(target)},
                exc_info=True,
            )
            return

    try:
        target.symlink_to(resolved_source, target_is_directory=True)
        return
    except OSError:
        logger.info(
            "Falling back to copy for workspace Gemini home sync",
            extra={"source": str(resolved_source), "target": str(target)},
        )

    try:
        shutil.copytree(resolved_source, target)
    except OSError:
        logger.warning(
            "Failed to sync Gemini home into workspace agent home",
            extra={"source": str(resolved_source), "target": str(target)},
            exc_info=True,
        )


def _apply_cleanup_outcome(result: WorkerResult, *, workspace_deleted: bool) -> WorkerResult:
    """Keep the reported result aligned with the final workspace state."""
    if not workspace_deleted:
        return result

    summary = (
        f"{result.summary.rstrip('.')} Workspace cleaned up per policy."
        if result.summary
        else "GeminiCliWorker cleaned up the workspace per policy."
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
        "Gemini CLI worker failed inside a provisioned workspace",
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


class GeminiCliWorkerNativeMixin:
    """Extracted mixin methods for Gemini CLI worker."""

    def _build_native_command(
        self,
        *,
        request: WorkerRequest,
        runtime_mode: WorkerRuntimeMode,
    ) -> list[str]:
        """Build a one-shot Gemini headless command for native-agent mode."""
        executable = getattr(self.runtime_adapter, "executable", "gemini")  # type: ignore[attr-defined]
        model = getattr(self.runtime_adapter, "model", None)  # type: ignore[attr-defined]
        read_only_requested = request.read_only or bool(request.constraints.get("read_only"))
        is_native = runtime_mode == WorkerRuntimeMode.NATIVE_AGENT
        approval_mode = (
            "plan"
            if read_only_requested
            else ("yolo" if is_native else DEFAULT_GEMINI_NATIVE_APPROVAL_MODE)
        )
        command = [
            executable,
            "--output-format",
            request.response_format if request.response_format == "json" else "text",
            "--approval-mode",
            approval_mode,
        ]
        if model:
            command.extend(["--model", str(model)])
        if self.native_sandbox_enabled and runtime_mode == WorkerRuntimeMode.NATIVE_AGENT:  # type: ignore[attr-defined]
            command.append("--sandbox")
        return command

    def _build_native_prompt(
        self, *, system_prompt: str, request: WorkerRequest, runtime_mode: WorkerRuntimeMode
    ) -> str:
        """Build the native-agent prompt packet for one-shot Gemini execution."""
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
            schema_json = json.dumps(request.response_schema, indent=2)
            output_instructions = (
                "Return exactly one JSON object that strictly matches this JSON schema:\n"
                f"{schema_json}\n"
                "Do not include markdown fences, explanatory prose, or extra keys."
            )

        role_instructions = ""
        if runtime_mode == WorkerRuntimeMode.REVIEWER_ONLY:
            role_instructions = (
                "## Specialist Role: Reviewer\n"
                "You are operating as a dedicated code reviewer. Focus on qualitative "
                "assessment, correctness, and architectural alignment. Do not perform "
                "implementation unless strictly required to verify a finding."
            )
        elif runtime_mode == WorkerRuntimeMode.PLANNER_ONLY:
            role_instructions = (
                "## Specialist Role: Planner\n"
                "You are operating as a dedicated task planner. Your goal is to break "
                "down the request into actionable steps. Do not execute the changes; "
                "provide the blueprint."
            )

        sections = [
            system_prompt.strip(),
            role_instructions,
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

        classified = classify_failure_kind(
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
        if classified == "unknown" and native_result.status == "error":
            return "provider_error"
        return classified

    def _native_run_env(self) -> dict[str, str] | None:
        """Build native run env with explicit sandbox/no-network defaults when possible."""
        base_env = dict(getattr(self.runtime_adapter, "env", {}) or {})  # type: ignore[attr-defined]
        if self.native_sandbox_enabled:  # type: ignore[attr-defined]
            base_env.setdefault("GEMINI_SANDBOX", "true")
            # On macOS seatbelt, prefer the no-network profile unless explicitly overridden.
            base_env.setdefault("SEATBELT_PROFILE", "permissive-closed")
        return base_env or None

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
        """Execute one native-agent Gemini run and map it into WorkerResult."""
        if cancel_token and cancel_token():
            return WorkerResult(
                status="error",
                summary="Gemini native-agent run was cancelled before execution.",
                failure_kind="timeout",
                artifacts=_workspace_artifacts(workspace),
                next_action_hint="inspect_workspace_artifacts",
            )

        system_prompt = (
            system_prompt_override
            if system_prompt_override is not None
            else build_system_prompt(
                request,
                workspace.repo_path,
                tool_registry=self.tool_registry,  # type: ignore[attr-defined]
            )
        )
        prompt = self._build_native_prompt(
            system_prompt=system_prompt, request=request, runtime_mode=runtime_mode
        )
        command = self._build_native_command(
            request=request,
            runtime_mode=runtime_mode,
        )
        native_result = run_native_agent(
            NativeAgentRunRequest(
                command=command,
                # Prompt is sent via stdin to avoid command-line length limits.
                prompt=prompt,
                repo_path=workspace.repo_path,
                workspace_path=workspace.workspace_path,
                timeout_seconds=runtime_settings.worker_timeout_seconds,
                diff_timeout_seconds=runtime_settings.command_timeout_seconds,
                changed_files_timeout_seconds=runtime_settings.command_timeout_seconds,
                env=self._native_run_env(),
                collect_diff=True,
                collect_changed_files=True,
                task_id=request.task_id,
                session_id=request.session_id,
                redactor=SecretRedactor(list((request.secrets or {}).values())),
                response_format=request.response_format,
                response_schema=request.response_schema,
            )
        )

        return self._build_worker_result_from_native_run(
            workspace, native_result, runtime_mode, cancel_token
        )

    def _build_worker_result_from_native_run(
        self,
        workspace: WorkspaceHandle,
        native_result: NativeAgentRunResult,
        runtime_mode: WorkerRuntimeMode,
        cancel_token: Callable[[], bool] | None,
    ) -> WorkerResult:
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
                    "sandbox_enabled": self.native_sandbox_enabled,  # type: ignore[attr-defined]
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
            result.summary = "Gemini native-agent run was cancelled by the orchestrator timeout."
            result.failure_kind = "timeout"
            result.next_action_hint = "inspect_workspace_artifacts"

        set_span_status_from_outcome(result.status, result.summary)
        return result
