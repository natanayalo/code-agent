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
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, Protocol
from uuid import uuid4

from sandbox.capability import FileSystemAccessPolicy, NetworkEgressPolicy
from sandbox.redact import SecretRedactor
from sandbox.scratch import node_agent_home, node_scratch_root
from sandbox.trusted_context import TrustedSandboxExecutionContext
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
        "BASH_ENV",
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
_SANDBOX_FILE_SECRET_ROOT: Final[PurePosixPath] = PurePosixPath("/run/secrets/code-agent")


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
        scratch_namespace: str | None,
        cancel_requested: Callable[[], bool] | None,
        redactor: SecretRedactor | None,
        context: TrustedSandboxExecutionContext | None,
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


def sandbox_file_secret_dir_for_request(
    workspace_path: Path, scratch_namespace: str | None
) -> Path:
    """Return the file-secret source directory outside writable container mounts."""
    return node_scratch_root(workspace_path, scratch_namespace) / "sandbox-secrets"


def _sandbox_file_secret_name(destination_mount_path: str) -> str:
    """Return the validated filename for a declared sandbox-file destination."""
    destination = PurePosixPath(destination_mount_path)
    if destination.parent != _SANDBOX_FILE_SECRET_ROOT or not destination.name:
        raise NativeAgentExecutorError(
            f"Invalid sandbox file secret destination: {destination_mount_path!r}"
        )
    return destination.name


def _sandbox_file_secret_mount(sandbox_secrets_dir: Path) -> str:
    """Build the read-only Docker bind mount for task-scoped file secrets."""
    return (
        "type=bind,source="
        f"{sandbox_secrets_dir.resolve()},target={_SANDBOX_FILE_SECRET_ROOT},readonly"
    )


def stage_agent_home_shell_environment(
    agent_home: Path,
    repo_path: Path,
) -> Path:
    """Stage dynamic task virtualenv discovery scripts in agent home."""
    env_script = agent_home / ".code_agent_env.sh"
    repo_venv_bin = repo_path.resolve() / ".venv" / "bin"
    script_content = (
        f'if [ -d "{repo_venv_bin}" ]; then\n'
        f'    case ":$PATH:" in\n'
        f'        *":{repo_venv_bin}:"*) ;;\n'
        f'        *) export PATH="{repo_venv_bin}:$PATH" ;;\n'
        f"    esac\n"
        f"fi\n"
    )
    env_script.write_text(script_content, encoding="utf-8")
    loader_snippet = (
        'if [ -f "$HOME/.code_agent_env.sh" ]; then\n    . "$HOME/.code_agent_env.sh"\nfi\n'
    )
    for startup_file in (agent_home / ".profile", agent_home / ".bashrc"):
        startup_file.write_text(loader_snippet, encoding="utf-8")
    return env_script


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
        resource_limits: Any,
        sandbox_secrets_dir: Path | None = None,
        network_name: str | None = None,
    ) -> list[str]:
        workspace_path = workspace.workspace_path.resolve()
        ro = ",readonly" if read_only_workspace else ""
        docker_command = [
            self.docker_binary,
            "run",
            "-i",
            "--name",
            container_name,
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            str(resource_limits.pids_limit),
            "--memory",
            str(resource_limits.memory_bytes),
            "--cpus",
            str(resource_limits.cpu_limit),
            "--ipc",
            "private",
            "--network",
            network_name or "none",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--mount",
            f"type=bind,source={workspace_path},target={workspace_path}{ro}",
        ]
        if read_only_workspace:
            docker_command.extend(
                [
                    "--mount",
                    (
                        "type=bind,source="
                        f"{workspace_path / '.code-agent'},target={workspace_path / '.code-agent'}"
                    ),
                ]
            )
        else:
            docker_command.extend(
                [
                    "--mount",
                    f"type=bind,source=/dev/null,target={workspace_path / '.git' / 'config'},readonly",  # noqa: E501
                    "--mount",
                    f"type=tmpfs,target={workspace_path / '.git' / 'hooks'},tmpfs-mode=0755,tmpfs-size=1m",  # noqa: E501
                ]
            )
        docker_command.extend(
            [
                "--mount",
                f"type=bind,source={artifact_root},target={artifact_root}",
                "--mount",
                f"type=bind,source={agent_home},target={agent_home}",
            ]
        )
        if sandbox_secrets_dir is not None:
            docker_command.extend(["--mount", _sandbox_file_secret_mount(sandbox_secrets_dir)])
        docker_command.extend(["--workdir", str(workspace.repo_path.resolve())])
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

    def _exited_container_code(self, container_name: str) -> int | None:
        """Return an exited executor's code without relying on its attached client."""
        inspected = self._docker(
            ["inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}", container_name]
        )
        if inspected.returncode != 0:
            return None
        running, _, exit_code = inspected.stdout.strip().partition(" ")
        if running == "true":
            return None
        try:
            return int(exit_code)
        except ValueError:
            return None

    def _docker(self, command: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.docker_binary, *command],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _start_proxy(
        self,
        *,
        network_name: str,
        artifact_root: Path,
        task_id: str,
        network_policy: str,
        allowed_hosts: list[str],
    ) -> str:
        proxy_name = f"native-egress-{uuid4().hex[:20]}"
        created = self._docker(["network", "create", "--internal", network_name])
        if created.returncode != 0:
            raise NativeAgentExecutorError(
                created.stderr.strip() or "failed to create executor network"
            )

        docker_args = [
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
            "--env",
            f"CODE_AGENT_NETWORK_POLICY={network_policy}",
        ]

        if allowed_hosts:
            docker_args.extend(["--env", f"CODE_AGENT_ALLOWED_HOSTS={','.join(allowed_hosts)}"])

        docker_args.extend(
            [
                self.image,
                "python",
                "/app/sandbox/native_agent_proxy.py",
            ]
        )

        proxy = self._docker(docker_args)
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
        scratch_namespace: str | None,
        cancel_requested: Callable[[], bool] | None,
        redactor: SecretRedactor | None,
        context: TrustedSandboxExecutionContext | None,
    ) -> NativeAgentExecution:
        if not context:
            raise NativeAgentExecutorError(
                "TrustedSandboxExecutionContext is required for native execution."
            )

        import threading

        from sandbox.provider_stager import ProviderCredentialStager
        from sandbox.redact import StreamingRedactor
        from sandbox.secrets import SecretScope

        artifact_root.mkdir(parents=True, exist_ok=True)
        agent_home = native_agent_home_for_request(workspace.workspace_path, scratch_namespace)
        sandbox_secrets_dir = sandbox_file_secret_dir_for_request(
            workspace.workspace_path, scratch_namespace
        )

        container_name = f"native-agent-{uuid4().hex[:20]}"

        network_enabled = context.grant.network in (
            NetworkEgressPolicy.ALLOWLISTED_HOSTS,
            NetworkEgressPolicy.PUBLIC_HTTPS_PROXY,
        )
        read_only_workspace = context.grant.filesystem != FileSystemAccessPolicy.WORKSPACE_WRITE
        network_name = f"native-agent-net-{uuid4().hex[:20]}" if network_enabled else None
        proxy_name: str | None = None

        try:
            resolver = context.secret_resolver

            provider_resolved_secrets = []
            agent_home.mkdir(parents=True, exist_ok=True)
            sandbox_secrets_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(sandbox_secrets_dir, 0o750)
            try:
                os.chown(sandbox_secrets_dir, 0, 65532)
            except OSError:
                pass

            scoped_env = build_executor_environment(
                {**environment},
                allow_github_credentials=False,
            )

            for handle in context.grant.allowed_secret_refs:
                val = resolver.resolve_for_sandbox(handle, context.grant)
                if val.scope == SecretScope.PROVIDER_AUTH:
                    provider_resolved_secrets.append(val)
                elif val.destination_mount_path:
                    secret_path = sandbox_secrets_dir / _sandbox_file_secret_name(
                        val.destination_mount_path
                    )
                    secret_path.write_text(val.reveal_secret_value(), encoding="utf-8")
                    try:
                        os.chown(secret_path, 0, 65532)
                    except OSError:
                        pass
                    try:
                        os.chmod(secret_path, 0o440)
                    except OSError:
                        pass

                if val.destination_env_var:
                    scoped_env[val.destination_env_var] = val.reveal_secret_value()

            ProviderCredentialStager.stage(
                provider_resolved_secrets,
                destination_by_ref=context.provider_bootstrap.destination_by_ref,
                task_home=agent_home,
            )
            env_script = stage_agent_home_shell_environment(agent_home, workspace.repo_path)
            scoped_env["BASH_ENV"] = str(env_script)

            # Change ownership of task home and workspace for the 65532 user
            def _chown_recursive(path: Path) -> None:
                if not path.exists():
                    return
                for root, dirs, files in os.walk(path):
                    # Exclude .git directory from being chowned
                    if ".git" in dirs:
                        dirs.remove(".git")
                    for d in dirs:
                        try:
                            os.chown(os.path.join(root, d), 65532, 65532)
                        except OSError:
                            pass
                    for f in files:
                        try:
                            os.chown(os.path.join(root, f), 65532, 65532)
                        except OSError:
                            pass
                try:
                    os.chown(path, 65532, 65532)
                except OSError:
                    pass

            _chown_recursive(agent_home)
            if not read_only_workspace:
                _chown_recursive(workspace.workspace_path)

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
                    network_policy=context.grant.network.value,
                    allowed_hosts=list(context.grant.allowed_egress_hosts)
                    if context.grant.allowed_egress_hosts
                    else [],
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
                sandbox_secrets_dir=sandbox_secrets_dir,
                environment=scoped_env,
                read_only_workspace=read_only_workspace,
                resource_limits=context.grant.resource_limits,
                network_name=network_name,
            )
        except (OSError, subprocess.SubprocessError, NativeAgentExecutorError) as exc:
            cleanup = self._cleanup_network(network_name=network_name, proxy_name=proxy_name)
            shutil.rmtree(agent_home, ignore_errors=True)
            shutil.rmtree(sandbox_secrets_dir, ignore_errors=True)
            manifest = {
                "execution_backend": "docker_native_agent_executor",
                "image": self.image,
                "workspace_mode": "read_only" if read_only_workspace else "read_write",
                "network_policy": "public_https_via_private_proxy"
                if network_enabled
                else "disabled",
                "provider_auth_scope": "task_scoped_staged_home",
                "github_grant": False,  # Managed centrally by SandboxCapabilityGrant now
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
        cleanup_result: str | None = None

        def cleanup_network() -> str:
            nonlocal cleanup_result
            if cleanup_result is None:
                cleanup_result = self._cleanup_network(
                    network_name=network_name, proxy_name=proxy_name
                )
            return cleanup_result

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
            shutil.rmtree(sandbox_secrets_dir, ignore_errors=True)
            raise NativeAgentExecutorError(
                f"Failed to start isolated Docker executor: {exc}"
            ) from exc

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        stdout_redactor = StreamingRedactor(redactor)
        stderr_redactor = StreamingRedactor(redactor)

        def read_stream(
            stream: Any, chunks: list[str], streaming_redactor: StreamingRedactor
        ) -> None:
            if not stream:
                return
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    safe_chunk = streaming_redactor.push(chunk)
                    if safe_chunk:
                        chunks.append(safe_chunk)
                final_chunk = streaming_redactor.flush()
                if final_chunk:
                    chunks.append(final_chunk)
            except OSError:
                pass

        stdout_thread = threading.Thread(
            target=read_stream, args=(process.stdout, stdout_chunks, stdout_redactor), daemon=True
        )
        stderr_thread = threading.Thread(
            target=read_stream, args=(process.stderr, stderr_chunks, stderr_redactor), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        def collect_output(timeout: int = 15) -> tuple[str, str]:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            return "".join(stdout_chunks), "".join(stderr_chunks)

        try:
            last_container_check = started
            while process.poll() is None:
                now = time.monotonic()
                if now - last_container_check >= 1:
                    last_container_check = now
                    exit_code = self._exited_container_code(container_name)
                    if exit_code is not None:
                        process.terminate()
                        stdout, stderr = collect_output(timeout=15)
                        self._remove(container_name)
                        manifest.update(
                            {
                                "termination_reason": "completed",
                                "cleanup": cleanup_network(),
                            }
                        )
                        manifest_path.write_text(
                            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
                        )
                        return NativeAgentExecution(
                            completed=subprocess.CompletedProcess(
                                docker_command, exit_code, stdout, stderr
                            ),
                            termination_reason="completed",
                            manifest_path=manifest_path,
                        )
                if cancel_requested and cancel_requested():
                    self._remove(container_name)
                    stdout, stderr = collect_output(timeout=15)
                    manifest.update(
                        {
                            "termination_reason": "cancelled",
                            "cleanup": cleanup_network(),
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
                    stdout, stderr = collect_output(timeout=15)
                    manifest.update(
                        {
                            "termination_reason": "timeout",
                            "cleanup": cleanup_network(),
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
            stdout, stderr = collect_output(timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            self._remove(container_name)
            cleanup_network()
            raise NativeAgentExecutorError(f"Isolated Docker executor failed: {exc}") from exc
        finally:
            cleanup_network()
            shutil.rmtree(agent_home, ignore_errors=True)
            shutil.rmtree(sandbox_secrets_dir, ignore_errors=True)
        self._remove(container_name)
        manifest.update(
            {
                "termination_reason": "completed",
                "cleanup": cleanup_network(),
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
