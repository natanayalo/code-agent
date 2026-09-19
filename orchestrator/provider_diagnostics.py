from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from datetime import timedelta
from typing import Any

from db.base import utc_now
from orchestrator.provider_diagnostics_credentials import check_provider_credentials
from orchestrator.provider_diagnostics_types import (
    DEFAULT_DOCKER_PROBE_TIMEOUT_SECONDS,
    DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    PROBE_CACHE_TTL_SECONDS,
    DiagnosticCheckResult,
    ProviderExecutionContext,
    ProviderPreDispatchReport,
    SystemProviderDiagnosticsReport,
    compute_report_id,
)
from sandbox.native_agent_executor import DEFAULT_NATIVE_AGENT_IMAGE
from sandbox.secrets import DEFAULT_SECRET_REGISTRY, SecretRegistry
from workers.base import FailureKind, WorkerRequest

logger = logging.getLogger(__name__)

_PROBE_CACHE: dict[str, tuple[float, DiagnosticCheckResult]] = {}
_MAX_DETAIL_BYTES = 120


def _optional_string(value: Any) -> str | None:
    """Accept only real string metadata at the diagnostics model boundary."""
    return value if isinstance(value, str) else None


def _truncate_detail(value: str) -> str:
    """Keep operator-facing diagnostics bounded and safe to display."""
    if len(value) <= _MAX_DETAIL_BYTES:
        return value
    return f"{value[: _MAX_DETAIL_BYTES - 3]}..."


def _get_cached_check(cache_key: str) -> DiagnosticCheckResult | None:
    cached = _PROBE_CACHE.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < PROBE_CACHE_TTL_SECONDS:
        return cached[1]
    return None


def _set_cached_check(cache_key: str, check: DiagnosticCheckResult) -> None:
    _PROBE_CACHE[cache_key] = (time.monotonic(), check)


def clear_probe_cache() -> None:
    _PROBE_CACHE.clear()


def _resolve_executable(worker: Any, worker_type: str) -> str | None:
    executable = getattr(getattr(worker, "runtime_adapter", None), "executable", None)
    if not isinstance(executable, str) or not executable:
        if worker_type == "codex":
            executable = os.environ.get("CODE_AGENT_CODEX_CLI_BIN", "codex")
        elif worker_type == "antigravity":
            executable = os.environ.get("CODE_AGENT_ANTIGRAVITY_CLI_BIN", "agy")
    return executable


def _resolve_auth_mechanism(worker: Any, worker_type: str, credential_refs: tuple[str, ...]) -> str:
    if worker_type == "codex":
        adapter = getattr(worker, "runtime_adapter", None)
        if "openai_api_key" in [r.lower() for r in credential_refs] or (
            getattr(adapter, "auth_mode", None) == "api_key"
        ):
            return "api_key"
        return "chatgpt_oauth"
    if worker_type == "antigravity":
        return "antigravity_oauth"
    if worker_type == "openrouter":
        return "api_key"
    return "none"


def _worker_requires_sandbox_container(worker: Any, worker_type: str, runtime_mode: str) -> bool:
    if hasattr(worker, "container_manager"):
        return True
    return runtime_mode in ("native_agent", "shell") and worker_type in (
        "codex",
        "antigravity",
    )


def _resolve_executor_image(
    worker: Any, worker_type: str, runtime_mode: str, manifest_sandbox: dict[str, Any]
) -> str:
    manager = getattr(worker, "container_manager", None)
    worker_default = _optional_string(getattr(manager, "default_image", None))
    if worker_default and (worker_type == "openrouter" or runtime_mode in ("tool_loop", "shell")):
        return worker_default
    return (
        _optional_string(manifest_sandbox.get("native_executor_image"))
        or os.environ.get("CODE_AGENT_NATIVE_AGENT_EXECUTOR_IMAGE")
        or DEFAULT_NATIVE_AGENT_IMAGE
    )


def _extract_secret_refs(request: WorkerRequest) -> tuple[str, ...]:
    raw_refs = request.secret_refs or ()
    extracted = [ref.name if hasattr(ref, "name") else str(ref) for ref in raw_refs]
    return tuple(sorted(set(extracted)))


def resolve_execution_context(
    worker: Any,
    request: WorkerRequest,
    *,
    worker_type: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    attempt_count: int = 1,
    logical_execution_key: str | None = None,
) -> ProviderExecutionContext:
    """Derive trusted execution context from final WorkerRequest and worker adapter."""
    worker_type = str(
        worker_type
        or getattr(worker, "worker_type", None)
        or getattr(request, "worker_type", None)
        or (request.task_spec or {}).get("worker_type", "unknown")
    ).lower()

    manifest_worker = (request.runtime_manifest or {}).get("worker") or {}
    manifest_sandbox = (request.runtime_manifest or {}).get("sandbox") or {}

    worker_profile = (
        _optional_string(getattr(request, "worker_profile", None))
        or _optional_string(manifest_worker.get("worker_profile"))
        or _optional_string(getattr(getattr(worker, "runtime_adapter", None), "profile", None))
    )
    runtime_mode = str(
        getattr(request, "runtime_mode", None)
        or manifest_worker.get("runtime_mode")
        or getattr(worker, "default_runtime_mode", "native_agent")
    )
    model = (
        _optional_string(getattr(request, "model", None))
        or _optional_string(manifest_worker.get("model"))
        or _optional_string(getattr(getattr(worker, "runtime_adapter", None), "model", None))
    )
    reasoning_effort = (
        _optional_string(getattr(request, "reasoning_effort", None))
        or _optional_string(manifest_worker.get("reasoning_effort"))
        or _optional_string(
            getattr(getattr(worker, "runtime_adapter", None), "reasoning_effort", None)
        )
    )

    executable = _resolve_executable(worker, worker_type)
    executor_image = _resolve_executor_image(worker, worker_type, runtime_mode, manifest_sandbox)
    is_container = _worker_requires_sandbox_container(worker, worker_type, runtime_mode)
    credential_refs = _extract_secret_refs(request)
    auth_mech = _resolve_auth_mechanism(worker, worker_type, credential_refs)

    resolved_task_id = task_id or request.task_id or "task-unknown"
    resolved_key = (
        logical_execution_key
        or f"{resolved_task_id}:attempt:{attempt_count}:{worker_profile or 'default'}"
    )

    return ProviderExecutionContext(
        provider=worker_type,
        worker_profile=worker_profile,
        runtime_mode=runtime_mode,
        model=model,
        reasoning_effort=reasoning_effort,
        executable=executable,
        executor_image=executor_image,
        auth_mechanism=auth_mech,  # type: ignore[arg-type]
        credential_refs=credential_refs,
        is_container_execution=is_container,
        task_id=resolved_task_id,
        session_id=session_id or request.session_id,
        attempt_count=attempt_count,
        logical_execution_key=resolved_key,
    )


class ProviderDiagnosticsService:
    def __init__(
        self,
        *,
        secret_registry: SecretRegistry | None = None,
        docker_probe_timeout: float = DEFAULT_DOCKER_PROBE_TIMEOUT_SECONDS,
        preflight_timeout: float = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    ) -> None:
        self.secret_registry = (
            secret_registry if secret_registry is not None else DEFAULT_SECRET_REGISTRY
        )
        self.docker_probe_timeout = docker_probe_timeout
        self.preflight_timeout = preflight_timeout

    def check_credentials(self, context: ProviderExecutionContext) -> DiagnosticCheckResult:
        return check_provider_credentials(context, self.secret_registry)

    async def _probe_docker_process(self) -> tuple[int | None, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "info",
            "--format",
            "{{.ServerVersion}}",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.docker_probe_timeout
            )
            return proc.returncode, stdout.decode("utf-8").strip(), stderr.decode("utf-8").strip()
        except (TimeoutError, asyncio.CancelledError):
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            raise

    async def check_docker_daemon(self) -> DiagnosticCheckResult:
        """Asynchronously probe Docker daemon responsiveness with bounded timeout."""
        cache_key = "docker_daemon"
        cached = _get_cached_check(cache_key)
        if cached is not None:
            return cached

        if not shutil.which("docker"):
            res = DiagnosticCheckResult(
                name="docker_daemon",
                category="container_runtime",
                status="unready",
                detail="Docker CLI executable 'docker' was not found in host PATH.",
                remediation="Install Docker CLI and ensure it is on PATH.",
                verification_scope="local_runtime",
                blocking=True,
            )
            _set_cached_check(cache_key, res)
            return res

        try:
            code, out, err = await self._probe_docker_process()
            if code == 0:
                res = DiagnosticCheckResult(
                    name="docker_daemon",
                    category="container_runtime",
                    status="ready",
                    detail=f"Docker daemon is responsive (version {out}).",
                    verification_scope="local_runtime",
                    blocking=False,
                )
            else:
                err_summary = _truncate_detail(err.splitlines()[0] if err else f"exit code {code}")
                res = DiagnosticCheckResult(
                    name="docker_daemon",
                    category="container_runtime",
                    status="unready",
                    detail=f"Docker daemon unavailable: {err_summary}",
                    remediation=(
                        "Start Docker daemon and ensure the current user has socket access."
                    ),
                    verification_scope="local_runtime",
                    blocking=True,
                )
        except TimeoutError:
            res = DiagnosticCheckResult(
                name="docker_daemon",
                category="container_runtime",
                status="unknown",
                detail=f"Docker daemon probe timed out after {self.docker_probe_timeout}s.",
                remediation="Verify Docker daemon responsiveness and host resource contention.",
                verification_scope="local_runtime",
                blocking=True,
            )
        except Exception as exc:
            res = DiagnosticCheckResult(
                name="docker_daemon",
                category="container_runtime",
                status="unready",
                detail=f"Docker daemon probe failed: {type(exc).__name__}",
                remediation="Start Docker daemon.",
                verification_scope="local_runtime",
                blocking=True,
            )

        _set_cached_check(cache_key, res)
        return res

    async def _inspect_image_process(self, image_name: str) -> tuple[int | None, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "image",
            "inspect",
            image_name,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.docker_probe_timeout
            )
            return proc.returncode, stdout.decode("utf-8").strip(), stderr.decode("utf-8").strip()
        except (TimeoutError, asyncio.CancelledError):
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            raise

    async def check_executor_image(self, image_name: str) -> DiagnosticCheckResult:
        """Asynchronously verify local presence of the executor image."""
        cache_key = f"image:{image_name}"
        cached = _get_cached_check(cache_key)
        if cached is not None:
            return cached

        try:
            code, _, _ = await self._inspect_image_process(image_name)
            if code == 0:
                res = DiagnosticCheckResult(
                    name="executor_image",
                    category="container_runtime",
                    status="ready",
                    detail=f"Executor image '{image_name}' is available locally.",
                    verification_scope="local_runtime",
                    blocking=False,
                )
            else:
                res = DiagnosticCheckResult(
                    name="executor_image",
                    category="container_runtime",
                    status="unready",
                    detail=f"Executor image '{image_name}' was not found locally.",
                    remediation="Run 'docker compose build worker' to build the image.",
                    verification_scope="local_runtime",
                    blocking=True,
                )
        except TimeoutError:
            res = DiagnosticCheckResult(
                name="executor_image",
                category="container_runtime",
                status="unknown",
                detail=(
                    f"Image inspection for '{image_name}' timed out "
                    f"after {self.docker_probe_timeout}s."
                ),
                remediation="Check Docker daemon load and local image store.",
                verification_scope="local_runtime",
                blocking=True,
            )
        except Exception as exc:
            res = DiagnosticCheckResult(
                name="executor_image",
                category="container_runtime",
                status="unready",
                detail=f"Image inspection failed: {type(exc).__name__}",
                remediation="Run 'docker compose build worker'.",
                verification_scope="local_runtime",
                blocking=True,
            )

        _set_cached_check(cache_key, res)
        return res

    def check_cli_binary(self, context: ProviderExecutionContext) -> DiagnosticCheckResult:
        """Validate configured executable in host or container execution scope."""
        if context.provider == "openrouter":
            return DiagnosticCheckResult(
                name="cli_binary",
                category="cli_binary",
                status="ready",
                detail="OpenRouter uses HTTP API client; CLI binary check skipped.",
                verification_scope="local_runtime",
                blocking=False,
            )

        executable = context.executable or ("codex" if context.provider == "codex" else "agy")
        if context.is_container_execution:
            return DiagnosticCheckResult(
                name="cli_binary",
                category="cli_binary",
                status="ready",
                detail=(
                    f"Configured executable '{executable}' presence inside container image "
                    "is unverified at this stage."
                ),
                verification_scope="local_presence",
                blocking=False,
            )

        if not shutil.which(executable):
            return DiagnosticCheckResult(
                name="cli_binary",
                category="cli_binary",
                status="unready",
                detail=f"Provider executable '{executable}' not found on host PATH.",
                remediation=f"Install '{executable}' and add to PATH, or use container execution.",
                verification_scope="local_runtime",
                blocking=True,
            )

        return DiagnosticCheckResult(
            name="cli_binary",
            category="cli_binary",
            status="ready",
            detail=f"Executable '{executable}' found on host PATH.",
            verification_scope="local_runtime",
            blocking=False,
        )

    def _build_rejection_report(
        self,
        report_id: str,
        checked_at: Any,
        context: ProviderExecutionContext,
        checks: list[DiagnosticCheckResult],
        primary: DiagnosticCheckResult,
    ) -> ProviderPreDispatchReport:
        failure_kind: FailureKind = (
            "provider_auth"
            if primary.category == "credentials"
            else "sandbox_infra"
            if primary.category == "container_runtime"
            else "provider_error"
        )
        hint = primary.remediation or "inspect_worker_configuration"
        if primary.name == "docker_daemon":
            hint = "start_docker_daemon"
        elif primary.name == "executor_image":
            hint = "build_or_pull_executor_image"
        elif primary.name == "credentials":
            if context.provider == "codex":
                hint = "configure_codex_auth"
            elif context.provider == "antigravity":
                hint = "bootstrap_antigravity_auth"
            elif context.provider == "openrouter":
                hint = "configure_openrouter_api_key"

        return ProviderPreDispatchReport(
            report_id=report_id,
            checked_at=checked_at,
            execution_environment_id=os.environ.get("HOSTNAME", "local"),
            logical_execution_key=context.logical_execution_key or "unknown",
            provider=context.provider,
            worker_profile=context.worker_profile,
            runtime_mode=context.runtime_mode,
            model=context.model,
            decision="preflight_rejected",
            ready=False,
            execution_started=False,
            checks=checks,
            failure_kind=failure_kind,
            next_action_hint=hint,
            reason_code=f"{primary.name}_unready",
            summary=f"PRE_DISPATCH_DIAGNOSTIC_FAILURE: {primary.detail}",
        )

    async def evaluate_pre_dispatch(
        self, context: ProviderExecutionContext
    ) -> ProviderPreDispatchReport:
        """Run all applicable pre-dispatch checks against the execution context."""
        checked_at = utc_now()
        report_id = compute_report_id(context.logical_execution_key or "unknown")
        checks: list[DiagnosticCheckResult] = []

        try:
            checks.append(self.check_credentials(context))
            checks.append(self.check_cli_binary(context))

            if context.is_container_execution:
                docker_check = await self.check_docker_daemon()
                checks.append(docker_check)
                if docker_check.status == "ready" and context.executor_image:
                    checks.append(await self.check_executor_image(context.executor_image))

            blocking = [c for c in checks if c.blocking]
            if blocking:
                return self._build_rejection_report(
                    report_id, checked_at, context, checks, blocking[0]
                )

            return ProviderPreDispatchReport(
                report_id=report_id,
                checked_at=checked_at,
                execution_environment_id=os.environ.get("HOSTNAME", "local"),
                logical_execution_key=context.logical_execution_key or "unknown",
                provider=context.provider,
                worker_profile=context.worker_profile,
                runtime_mode=context.runtime_mode,
                model=context.model,
                decision="preflight_passed",
                ready=True,
                execution_started=False,
                checks=checks,
                summary="All pre-dispatch diagnostic checks passed.",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Unexpected error in pre-dispatch diagnostics: %s", exc)
            return ProviderPreDispatchReport(
                report_id=report_id,
                checked_at=checked_at,
                execution_environment_id=os.environ.get("HOSTNAME", "local"),
                logical_execution_key=context.logical_execution_key or "unknown",
                provider=context.provider,
                worker_profile=context.worker_profile,
                runtime_mode=context.runtime_mode,
                model=context.model,
                decision="preflight_error",
                ready=False,
                execution_started=False,
                checks=checks,
                failure_kind="sandbox_infra",
                next_action_hint="inspect_worker_configuration",
                reason_code="preflight_internal_error",
                summary=f"PRE_DISPATCH_INTERNAL_ERROR: {type(exc).__name__}",
            )

    async def evaluate_all_providers(
        self,
        required_providers: set[str] | None = None,
        target: str = "local",
        target_providers: set[str] | None = None,
    ) -> SystemProviderDiagnosticsReport:
        """Run diagnostics for selected providers and generate an operator snapshot."""
        now = utc_now()
        reports: dict[str, ProviderPreDispatchReport] = {}
        supported = ("codex", "antigravity", "openrouter")
        supported_set = set(supported)
        target_set = (
            {provider.lower() for provider in target_providers}
            if target_providers is not None
            else set(supported)
        )

        for prov in supported:
            if prov not in target_set:
                continue
            executable = "codex" if prov == "codex" else "agy" if prov == "antigravity" else None
            auth_mech = (
                "chatgpt_oauth"
                if prov == "codex"
                else "antigravity_oauth"
                if prov == "antigravity"
                else "api_key"
            )
            ctx = ProviderExecutionContext(
                provider=prov,
                worker_profile=None,
                runtime_mode="native_agent",
                model=None,
                reasoning_effort=None,
                executable=executable,
                executor_image=DEFAULT_NATIVE_AGENT_IMAGE,
                auth_mechanism=auth_mech,  # type: ignore[arg-type]
                credential_refs=(),
                is_container_execution=(prov in ("codex", "antigravity")),
                logical_execution_key=f"system_probe:{prov}",
            )
            reports[prov] = await self.evaluate_pre_dispatch(ctx)

        all_ready = (
            bool(target_set)
            and target_set <= supported_set
            and all(
                reports.get(provider) is not None and reports[provider].ready
                for provider in target_set
            )
        )
        req_set = (
            {provider.lower() for provider in required_providers}
            if required_providers is not None
            else target_set
        )
        required_set_valid = bool(req_set) and req_set <= supported_set
        req_ready = required_set_valid and all(
            reports.get(provider) is not None and reports[provider].ready for provider in req_set
        )

        return SystemProviderDiagnosticsReport(
            checked_at=now,
            expires_at=now + timedelta(seconds=PROBE_CACHE_TTL_SECONDS),
            target=target,
            execution_environment_id=os.environ.get("HOSTNAME", "local"),
            providers=reports,
            all_ready=all_ready,
            required_providers_ready=req_ready,
        )
