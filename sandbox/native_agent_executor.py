"""Hardened one-shot Docker execution for model-directed native CLIs.

The Temporal worker is deliberately the only component that talks to the
Docker daemon.  This module starts an unprivileged, task-scoped container for
the provider process; the provider container never receives the daemon socket
or control-plane environment.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol
from uuid import uuid4

from sandbox.redact import SecretRedactor
from sandbox.scratch import node_agent_home
from sandbox.workspace import WorkspaceHandle

DEFAULT_NATIVE_AGENT_IMAGE: Final[str] = "code-agent-worker"
DEFAULT_NATIVE_AGENT_MEMORY_LIMIT: Final[str] = "1g"
DEFAULT_NATIVE_AGENT_CPU_LIMIT: Final[float] = 1.0
DEFAULT_NATIVE_AGENT_PIDS_LIMIT: Final[int] = 256
_PROTECTED_ENV_PREFIXES: Final[tuple[str, ...]] = (
    "AWS_",
    "DATABASE_",
    "GCP_",
    "GH_",
    "GITHUB_",
    "OPENROUTER_",
    "OTEL_",
    "PHOENIX_",
    "POSTGRES_",
    "TEMPORAL_",
    "TELEGRAM_",
)
_PROTECTED_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {"CODE_AGENT_API_SHARED_SECRET", "DOCKER_HOST", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
)
_EXECUTOR_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {
        "AGY_CLI_DISABLE_AUTO_UPDATE",
        "CODEX_HOME",
        "FORCE_COLOR",
        "GEMINI_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PATH",
        "SSL_CERT_FILE",
        "TERM",
        "TZ",
    }
)


class NativeAgentExecutorError(RuntimeError):
    """Raised when isolated native-agent execution cannot be established."""


class NativeAgentProcessRunner(Protocol):
    """Internal process boundary used by the provider-independent runner."""

    def run(
        self,
        *,
        command: list[str],
        prompt: str | None,
        workspace: WorkspaceHandle,
        artifact_root: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
        read_only_workspace: bool,
        scratch_namespace: str | None,
        cancel_requested: Callable[[], bool] | None,
        redactor: SecretRedactor | None,
        network_enabled: bool,
        github_credentials: Mapping[str, str],
        provider_auth_source: Path | None,
    ) -> NativeAgentExecution: ...


@dataclass(frozen=True)
class NativeAgentExecution:
    """Result of the container process without provider-specific interpretation."""

    completed: subprocess.CompletedProcess[str]
    termination_reason: Literal["completed", "timeout", "cancelled", "startup_error"]
    manifest_path: Path


def _is_protected_environment_key(key: str) -> bool:
    upper = key.upper()
    return upper in _PROTECTED_ENV_KEYS or upper.startswith(_PROTECTED_ENV_PREFIXES)


def build_executor_environment(
    environment: Mapping[str, str],
    *,
    allow_github_credentials: bool = False,
) -> dict[str, str]:
    """Return the explicit native-container environment allowlist.

    Provider authentication is file based and mounted separately.  GitHub
    credentials are intentionally absent unless the caller established the
    existing explicit tool/permission grant.
    """
    scoped = {
        key: value
        for key, value in environment.items()
        if key in _EXECUTOR_ENV_KEYS and not _is_protected_environment_key(key)
    }
    if allow_github_credentials:
        for key in ("GH_TOKEN", "GITHUB_TOKEN"):
            value = environment.get(key)
            if value:
                scoped[key] = value
    return scoped


def provider_auth_home_for_environment(environment: Mapping[str, str]) -> Path | None:
    """Choose exactly one provider home to stage for the current invocation."""
    for key in ("CODEX_HOME", "GEMINI_HOME"):
        value = environment.get(key)
        if value:
            return Path(value).expanduser()
    return None


def provider_home_name(
    *,
    command: list[str],
    provider_auth_source: Path | None,
) -> str:
    """Choose the staged provider directory without ambiguous HOME variables."""
    if provider_auth_source is not None and provider_auth_source.name in {".codex", ".gemini"}:
        return provider_auth_source.name
    executable = Path(command[0]).name if command else ""
    return ".codex" if executable == "codex" else ".gemini"


def native_agent_home_for_request(workspace_path: Path, scratch_namespace: str | None) -> Path:
    """Return the provider home selected by native command construction.

    Antigravity writes its generated settings before the executor starts, so
    the executor must mount this exact path rather than inventing a second
    per-task home.  A namespace is always supplied by the durable worker; the
    workspace fallback preserves the legacy direct-worker invocation path.
    """
    if scratch_namespace:
        return node_agent_home(workspace_path, scratch_namespace)
    return workspace_path / ".agent_home"


def stage_provider_auth(
    *,
    source: Path | None,
    destination: Path,
) -> None:
    """Copy a minimal provider auth surface into task-private scratch."""
    destination.mkdir(parents=True, exist_ok=True)
    if source is None:
        return
    for name in (
        "auth.json",
        "config.toml",
        "settings.json",
        "google_accounts.json",
        "oauth_creds.json",
        "projects.json",
        "state.json",
        "installation_id",
        "trustedFolders.json",
        "config/config.json",
        "config/projects/default-cli-project.json",
        "antigravity/antigravity_state.pbtxt",
        "antigravity/installation_id",
        "antigravity-cli/antigravity-oauth-token",
        "antigravity-cli/jetski_state.pbtxt",
    ):
        candidate = source / name
        target = destination / name
        if candidate.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Native command preparation may have created a worker-local link
            # to the source OAuth token.  That link deliberately cannot work
            # in the executor, which never mounts the trusted worker home.
            # Replace it before copying so the task home contains a real,
            # private credential file rather than a dangling host reference.
            if target.is_symlink():
                target.unlink()
            shutil.copy2(candidate, target)
            target.chmod(0o600)


def is_public_egress_host(host: str, resolved_addresses: list[str]) -> bool:
    """Validate proxy resolution results without trusting DNS alone."""
    if not host or host.lower() in {
        "localhost",
        "host.docker.internal",
        "metadata.google.internal",
    }:
        return False
    try:
        values = [ipaddress.ip_address(value) for value in resolved_addresses]
    except ValueError:
        return False
    return bool(values) and all(value.is_global for value in values)


class DockerNativeAgentExecutor:
    """Run provider CLIs in a one-shot hardened Docker container.

    The executor joins only a task-private internal network.  Its companion
    CONNECT proxy joins that internal network and Docker's public bridge; this
    is the sole path to public HTTPS.
    """

    def __init__(
        self,
        *,
        image: str | None = None,
        docker_binary: str = "docker",
        memory_limit: str = DEFAULT_NATIVE_AGENT_MEMORY_LIMIT,
        cpu_limit: float = DEFAULT_NATIVE_AGENT_CPU_LIMIT,
        pids_limit: int = DEFAULT_NATIVE_AGENT_PIDS_LIMIT,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self.image: str = (
            image
            or os.environ.get("CODE_AGENT_NATIVE_AGENT_EXECUTOR_IMAGE")
            or DEFAULT_NATIVE_AGENT_IMAGE
        )
        self.docker_binary = docker_binary
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.pids_limit = pids_limit
        self.poll_interval_seconds = poll_interval_seconds

    def build_run_command(
        self,
        *,
        container_name: str,
        command: list[str],
        workspace: WorkspaceHandle,
        artifact_root: Path,
        agent_home: Path,
        environment: Mapping[str, str],
        read_only_workspace: bool,
        network_name: str | None = None,
    ) -> list[str]:
        workspace_path = workspace.workspace_path.resolve()
        ro = ",readonly" if read_only_workspace else ""
        docker_command = [
            self.docker_binary,
            "run",
            "--rm",
            "-i",
            "--name",
            container_name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory_limit,
            "--cpus",
            str(self.cpu_limit),
            "--ipc",
            "private",
            "--network",
            network_name or "none",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--mount",
            f"type=bind,source={workspace_path},target={workspace_path}{ro}",
            "--mount",
            f"type=bind,source={artifact_root},target={artifact_root}",
            "--mount",
            f"type=bind,source={agent_home},target={agent_home}",
            "--workdir",
            str(workspace.repo_path.resolve()),
        ]
        for key, value in sorted(environment.items()):
            docker_command.extend(["--env", f"{key}={value}"])
        docker_command.append(self.image)
        docker_command.extend(command)
        return docker_command

    def _remove(self, container_name: str) -> None:
        subprocess.run(
            [self.docker_binary, "rm", "-f", container_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def _docker(self, command: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.docker_binary, *command],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _start_proxy(self, *, network_name: str, artifact_root: Path, task_id: str) -> str:
        proxy_name = f"native-egress-{uuid4().hex[:20]}"
        created = self._docker(["network", "create", "--internal", network_name])
        if created.returncode != 0:
            raise NativeAgentExecutorError(
                created.stderr.strip() or "failed to create executor network"
            )
        proxy = self._docker(
            [
                "run",
                "-d",
                "--rm",
                "--name",
                proxy_name,
                "--network",
                network_name,
                "--network-alias",
                "native-egress-proxy",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--pids-limit",
                "128",
                "--memory",
                "128m",
                "--cpus",
                "0.25",
                "--ipc",
                "private",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                "--mount",
                f"type=bind,source={artifact_root},target={artifact_root}",
                "--env",
                f"CODE_AGENT_PROXY_AUDIT_PATH={artifact_root / 'egress-audit.jsonl'}",
                "--env",
                f"CODE_AGENT_PROXY_TASK_ID={task_id}",
                self.image,
                "python",
                "/app/sandbox/native_agent_proxy.py",
            ]
        )
        if proxy.returncode != 0:
            self._docker(["network", "rm", network_name])
            message = proxy.stderr.strip() or "failed to start egress proxy"
            raise NativeAgentExecutorError(message)
        connected = self._docker(["network", "connect", "bridge", proxy_name])
        if connected.returncode != 0:
            self._remove(proxy_name)
            self._docker(["network", "rm", network_name])
            raise NativeAgentExecutorError(
                connected.stderr.strip() or "failed to attach egress proxy"
            )
        return proxy_name

    def _cleanup_network(self, *, network_name: str | None, proxy_name: str | None) -> str:
        failures: list[str] = []
        if proxy_name:
            try:
                self._remove(proxy_name)
            except (OSError, subprocess.SubprocessError):
                failures.append("proxy")
        if network_name:
            try:
                if self._docker(["network", "rm", network_name]).returncode != 0:
                    failures.append("network")
            except (OSError, subprocess.SubprocessError):
                failures.append("network")
        return "removed" if not failures else f"cleanup_failed:{','.join(failures)}"

    def run(
        self,
        *,
        command: list[str],
        prompt: str | None,
        workspace: WorkspaceHandle,
        artifact_root: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
        read_only_workspace: bool,
        scratch_namespace: str | None,
        cancel_requested: Callable[[], bool] | None,
        redactor: SecretRedactor | None,
        network_enabled: bool,
        github_credentials: Mapping[str, str],
        provider_auth_source: Path | None,
    ) -> NativeAgentExecution:
        del redactor
        artifact_root.mkdir(parents=True, exist_ok=True)
        agent_home = native_agent_home_for_request(workspace.workspace_path, scratch_namespace)
        source_home = provider_auth_source or provider_auth_home_for_environment(environment)
        provider_home = agent_home / provider_home_name(
            command=command,
            provider_auth_source=provider_auth_source,
        )
        container_name = f"native-agent-{uuid4().hex[:20]}"
        network_name = f"native-agent-net-{uuid4().hex[:20]}" if network_enabled else None
        proxy_name: str | None = None
        try:
            stage_provider_auth(source=source_home, destination=provider_home)
            scoped_env = build_executor_environment(
                {**environment, **github_credentials},
                allow_github_credentials=bool(github_credentials),
            )
            scoped_env.update(
                {
                    "HOME": str(agent_home),
                    "CODEX_HOME": str(agent_home / ".codex"),
                    "GEMINI_HOME": str(agent_home / ".gemini"),
                }
            )
            if network_enabled:
                proxy_name = self._start_proxy(
                    network_name=network_name or "",
                    artifact_root=artifact_root,
                    task_id=workspace.task_id,
                )
                scoped_env.update(
                    {
                        "HTTP_PROXY": "http://native-egress-proxy:8080",
                        "HTTPS_PROXY": "http://native-egress-proxy:8080",
                        "NO_PROXY": "",
                    }
                )
            docker_command = self.build_run_command(
                container_name=container_name,
                command=command,
                workspace=workspace,
                artifact_root=artifact_root,
                agent_home=agent_home,
                environment=scoped_env,
                read_only_workspace=read_only_workspace,
                network_name=network_name,
            )
        except (OSError, subprocess.SubprocessError, NativeAgentExecutorError) as exc:
            cleanup = self._cleanup_network(network_name=network_name, proxy_name=proxy_name)
            shutil.rmtree(agent_home, ignore_errors=True)
            manifest = {
                "execution_backend": "docker_native_agent_executor",
                "image": self.image,
                "workspace_mode": "read_only" if read_only_workspace else "read_write",
                "network_policy": "public_https_via_private_proxy"
                if network_enabled
                else "disabled",
                "provider_auth_scope": "task_scoped_staged_home",
                "github_grant": bool(github_credentials),
                "termination_reason": "startup_error",
                "cleanup": cleanup,
            }
            manifest_path = artifact_root / "native-isolation-manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            raise NativeAgentExecutorError(f"Failed to establish isolated executor: {exc}") from exc
        started = time.monotonic()
        manifest = {
            "execution_backend": "docker_native_agent_executor",
            "image": self.image,
            "workspace_mode": "read_only" if read_only_workspace else "read_write",
            "network_policy": "public_https_via_private_proxy" if network_enabled else "disabled",
            "provider_auth_scope": "task_scoped_staged_home",
            "github_grant": bool(scoped_env.get("GH_TOKEN") or scoped_env.get("GITHUB_TOKEN")),
            "container_name": container_name,
        }
        manifest_path = artifact_root / "native-isolation-manifest.json"
        try:
            process = subprocess.Popen(
                docker_command,
                stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if process.stdin is not None:
                process.stdin.write(prompt or "")
                process.stdin.close()
                process.stdin = None
        except OSError as exc:
            manifest.update(
                {
                    "termination_reason": "startup_error",
                    "cleanup": self._cleanup_network(
                        network_name=network_name, proxy_name=proxy_name
                    ),
                }
            )
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            shutil.rmtree(agent_home, ignore_errors=True)
            raise NativeAgentExecutorError(
                f"Failed to start isolated Docker executor: {exc}"
            ) from exc
        try:
            while process.poll() is None:
                if cancel_requested and cancel_requested():
                    self._remove(container_name)
                    stdout, stderr = process.communicate(timeout=15)
                    manifest.update(
                        {
                            "termination_reason": "cancelled",
                            "cleanup": self._cleanup_network(
                                network_name=network_name, proxy_name=proxy_name
                            ),
                        }
                    )
                    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
                    return NativeAgentExecution(
                        completed=subprocess.CompletedProcess(
                            docker_command, process.returncode or 137, stdout, stderr
                        ),
                        termination_reason="cancelled",
                        manifest_path=manifest_path,
                    )
                if time.monotonic() - started > timeout_seconds:
                    self._remove(container_name)
                    stdout, stderr = process.communicate(timeout=15)
                    manifest.update(
                        {
                            "termination_reason": "timeout",
                            "cleanup": self._cleanup_network(
                                network_name=network_name, proxy_name=proxy_name
                            ),
                        }
                    )
                    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
                    return NativeAgentExecution(
                        completed=subprocess.CompletedProcess(
                            docker_command, process.returncode or 137, stdout, stderr
                        ),
                        termination_reason="timeout",
                        manifest_path=manifest_path,
                    )
                time.sleep(self.poll_interval_seconds)
            stdout, stderr = process.communicate(timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            self._remove(container_name)
            self._cleanup_network(network_name=network_name, proxy_name=proxy_name)
            raise NativeAgentExecutorError(f"Isolated Docker executor failed: {exc}") from exc
        finally:
            shutil.rmtree(agent_home, ignore_errors=True)
        manifest.update(
            {
                "termination_reason": "completed",
                "cleanup": self._cleanup_network(network_name=network_name, proxy_name=proxy_name),
            }
        )
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return NativeAgentExecution(
            completed=subprocess.CompletedProcess(
                docker_command, process.returncode, stdout, stderr
            ),
            termination_reason="completed",
            manifest_path=manifest_path,
        )


def native_executor_workspace_handle(
    *, workspace_path: Path, repo_path: Path, task_id: str | None
) -> WorkspaceHandle:
    """Build a minimal workspace handle for the runner's existing call sites."""
    from sandbox.workspace import WorkspaceCleanupPolicy

    return WorkspaceHandle(
        workspace_id=f"native-{task_id or 'unknown'}",
        task_id=task_id or "native-agent",
        workspace_path=workspace_path,
        repo_path=repo_path,
        repo_url="",
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )
