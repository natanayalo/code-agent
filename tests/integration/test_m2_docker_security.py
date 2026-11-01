"""Opt-in real Docker tests. Only owned synthetic resources; no daemon socket mounts.

Run on Linux as root to reproduce the trusted broker's file ownership setup:
CODE_AGENT_M2_DOCKER_TESTS=1 .venv/bin/pytest -q tests/integration/test_m2_docker_security.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from sandbox.capability import FileSystemAccessPolicy, NetworkEgressPolicy, SandboxCapabilityGrant
from sandbox.container import DockerSandboxContainerManager, DockerSandboxContainerRequest
from sandbox.native_agent_executor import (
    DockerNativeAgentExecutor,
)
from sandbox.provider_bootstrap import ProviderBootstrap
from sandbox.runner import DockerSandboxCommand, DockerSandboxRunner
from sandbox.secrets import ResolvedSecret, SecretScope
from sandbox.trusted_context import TrustedSandboxExecutionContext
from tests.unit.test_m2_execution_security import workspace

pytestmark = pytest.mark.skipif(
    os.environ.get("CODE_AGENT_M2_DOCKER_TESTS") != "1",
    reason="explicit synthetic Docker opt-in required",
)
NGINX = (
    "nginx:stable-alpine@sha256:47928bf67eb62a1863cb08d9ecb52d3e704304ba614eaee17884204677702951"
)
ROOT = Path(__file__).resolve().parents[2]


def docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=180
    )


def await_http(url):
    for _ in range(100):
        try:
            return httpx.get(url, timeout=1)
        except httpx.TransportError:
            time.sleep(0.1)
    raise AssertionError("synthetic HTTP listener did not become ready")


@pytest.fixture(scope="module")
def synthetic_image(tmp_path_factory):
    build = tmp_path_factory.mktemp("m2-image")
    (build / "Dockerfile").write_text(
        "FROM python:3.12-slim\nCOPY native_agent_proxy.py /app/sandbox/native_agent_proxy.py\n"
    )
    shutil.copyfile(ROOT / "sandbox/native_agent_proxy.py", build / "native_agent_proxy.py")
    image = "code-agent-m2-test:" + uuid4().hex[:12]
    docker(
        "build", "-q", "--label", "code-agent.m2.owner=security-validation", "-t", image, str(build)
    )
    try:
        metadata = json.loads(docker("image", "inspect", image).stdout)[0]
        evidence = ROOT / "artifacts/m2-runtime-identities.json"
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text(
            json.dumps(
                {
                    "synthetic_image_id": metadata["Id"],
                    "synthetic_tag": image,
                    "ingress_image": NGINX,
                    "base_commit": subprocess.check_output(
                        ["git", "-c", f"safe.directory={ROOT}", "rev-parse", "HEAD"], text=True
                    ).strip(),
                },
                indent=2,
            )
        )
        yield image
    finally:
        docker("image", "rm", image)


@pytest.fixture(scope="module")
def protected_volume(synthetic_image):
    name = "code-agent-m2-protected-" + uuid4().hex[:12]
    docker("volume", "create", "--label", "code-agent.m2.owner=security-validation", name)
    try:
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--mount",
            f"type=volume,source={name},target=/protected",
            synthetic_image,
            "python",
            "-c",
            "from pathlib import Path;Path('/protected/canary').write_text('synthetic-protected')",
        )
        yield name
        result = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--mount",
            f"type=volume,source={name},target=/protected,readonly",
            synthetic_image,
            "python",
            "-c",
            "from pathlib import Path;"
            "assert Path('/protected/canary').read_text()=='synthetic-protected'",
        )
        assert result.returncode == 0
    finally:
        docker("volume", "rm", name)


UPSTREAM_SERVER = (
    "from http.server import BaseHTTPRequestHandler,HTTPServer\n"
    "class H(BaseHTTPRequestHandler):\n"
    " def do_POST(self):\n"
    "  self.rfile.read(int(self.headers.get('Content-Length','0')))\n"
    "  print('UPSTREAM_POST',flush=True)\n"
    "  self.send_response(200);self.end_headers();self.wfile.write(b'accepted')\n"
    " def log_message(self,*args):pass\n"
    "HTTPServer(('0.0.0.0',8000),H).serve_forever()\n"
)


@pytest.fixture
def ingress_pair(synthetic_image):
    suffix = uuid4().hex[:12]
    network, api, proxy = (f"m2-{kind}-{suffix}" for kind in ("net", "api", "ingress"))
    upstream = network + "-private"
    docker("network", "create", network)
    docker("network", "create", "--internal", upstream)
    try:
        docker(
            "run",
            "-d",
            "--name",
            api,
            "--network",
            upstream,
            "--network-alias",
            "api",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--user",
            "65532:65532",
            synthetic_image,
            "python",
            "-c",
            UPSTREAM_SERVER,
        )
        docker(
            "run",
            "-d",
            "--name",
            proxy,
            "--network",
            network,
            "--network",
            upstream,
            "--publish",
            "127.0.0.1::8080",
            "--user",
            "101:101",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--memory",
            "64m",
            "--cpus",
            "0.25",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m,uid=101,gid=101",
            "--mount",
            "type=bind,source="
            + str(ROOT / "deploy/telegram/nginx.conf")
            + ",target=/etc/nginx/nginx.conf,readonly",
            "--entrypoint",
            "nginx",
            NGINX,
            "-g",
            "daemon off;",
        )
        metadata = json.loads(docker("inspect", proxy).stdout)[0]
        assert "8080/tcp" in metadata["NetworkSettings"]["Ports"], (
            metadata["State"],
            docker("logs", proxy).stderr,
        )
        port = metadata["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
        base = f"http://127.0.0.1:{port}"
        yield base, api
    finally:
        for name in (proxy, api):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        docker("network", "rm", network)
        docker("network", "rm", upstream)


def test_real_proxy_rejects_noncanonical_requests_before_upstream(ingress_pair):
    base, api = ingress_pair
    assert await_http(base + "/").status_code == 404
    paths = [
        "/",
        "/tasks",
        "/tasks/id/approval",
        "/tasks/id/replay",
        "/sessions",
        "/webhook",
        "/auth/login",
        "/auth/status",
        "/auth/logout",
        "/system/provider-diagnostics",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/health",
        "/telegram/webhook/",
        "/telegram/%77ebhook",
        "//telegram/webhook",
        "/telegram/webhook?x=1",
    ]
    for path in paths:
        assert httpx.post(base + path, content=b"{}").status_code in {400, 404}
    for method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"):
        assert httpx.request(method, base + "/telegram/webhook").status_code == 405
    assert (
        httpx.post(base + "/telegram/webhook", content=b"x" * (256 * 1024 + 1)).status_code == 413
    )
    assert "UPSTREAM_POST" not in docker("logs", api).stdout
    assert httpx.post(base + "/telegram/webhook", content=b"{}").status_code == 200
    assert docker("logs", api).stdout.count("UPSTREAM_POST") == 1


def test_native_auth_is_immutable_and_siblings_unavailable(
    tmp_path, synthetic_image, protected_volume
):
    if os.geteuid() != 0:
        pytest.skip("root test harness required for broker-equivalent ownership staging")
    handle = workspace(tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    sibling = tmp_path / "protected-sibling"
    sibling.write_text("synthetic-protected-canary")
    resolved = ResolvedSecret(name="auth", scope=SecretScope.PROVIDER_AUTH, value="synthetic-auth")
    context = TrustedSandboxExecutionContext(
        task_id=handle.task_id,
        grant=SandboxCapabilityGrant(
            filesystem=FileSystemAccessPolicy.WORKSPACE_WRITE,
            network=NetworkEgressPolicy.DISABLED,
            allowed_secret_refs=frozenset({"auth"}),
            granted_secret_scopes=frozenset({SecretScope.PROVIDER_AUTH}),
        ),
        secret_resolver=type("Resolver", (), {"resolve_for_sandbox": lambda *args: resolved})(),
        provider_bootstrap=ProviderBootstrap([], {}, {"auth": ".codex/auth.json"}, ("auth",)),
    )
    script = (
        "import os,pathlib,socket\n"
        "assert os.getuid()==65532\n"
        "p=pathlib.Path(os.environ['CODEX_HOME'])/'auth.json'\n"
        "assert p.read_text()=='synthetic-auth'\n"
        "try:p.write_text('tamper');raise AssertionError('credential writable')\n"
        "except OSError:pass\n"
        f"assert not pathlib.Path({str(sibling)!r}).exists()\n"
        f"assert not pathlib.Path('/var/lib/docker/volumes/{protected_volume}').exists()\n"
        "assert not pathlib.Path('/var/run/docker.sock').exists()\n"
        "assert not os.access('/root/.codex/auth.json',os.R_OK)\n"
        "assert 'GH_TOKEN' not in os.environ and 'DATABASE_URL' not in os.environ\n"
        "status=pathlib.Path('/proc/self/status').read_text()\n"
        "assert 'CapEff:\\t0000000000000000' in status and 'NoNewPrivs:\\t1' in status\n"
        "try:pathlib.Path('/m2-write').touch();raise AssertionError('root writable')\n"
        "except OSError:pass\n"
        "pathlib.Path('synthetic-result.txt').write_text('ok')\nprint('M2_NATIVE_PASS')\n"
    )
    execution = DockerNativeAgentExecutor(image=synthetic_image).run(
        command=["python", "-c", script],
        prompt=None,
        workspace=handle,
        artifact_root=artifacts,
        environment={"GH_TOKEN": "do-not-inherit", "DATABASE_URL": "do-not-inherit"},
        timeout_seconds=30,
        scratch_namespace="m2",
        cancel_requested=None,
        redactor=None,
        context=context,
    )
    assert execution.completed.returncode == 0, execution.completed.stderr
    assert "M2_NATIVE_PASS" in execution.completed.stdout
    assert sibling.read_text() == "synthetic-protected-canary"
    manifest = json.loads(execution.manifest_path.read_text())
    assert manifest["cleanup"] == "removed"
    assert (handle.repo_path / "synthetic-result.txt").read_text() == "ok"


@pytest.mark.parametrize("persistent", [False, True])
def test_legacy_real_privileges_and_task_network(
    tmp_path, synthetic_image, monkeypatch, persistent, protected_volume
):
    monkeypatch.setenv("CODE_AGENT_SANDBOX_IMAGE", synthetic_image)
    handle = workspace(tmp_path)
    script = (
        "import os,pathlib,socket,urllib.request\n"
        "assert os.getuid()!=0\n"
        "assert not pathlib.Path('/var/run/docker.sock').exists()\n"
        f"assert not pathlib.Path('/var/lib/docker/volumes/{protected_volume}').exists()\n"
        "assert 'CapEff:\\t0000000000000000' in pathlib.Path('/proc/self/status').read_text()\n"
        "try:pathlib.Path('/m2-write').touch();raise AssertionError('root writable')\n"
        "except OSError:pass\n"
        "targets=[('host.docker.internal',2375),('host.docker.internal',8090),('1.1.1.1',443)]\n"
        "for host,port in targets:\n"
        " try:\n"
        "  socket.create_connection((host,port),timeout=1).close()\n"
        "  raise AssertionError('direct egress')\n"
        " except OSError:pass\n"
        "proxy=urllib.request.ProxyHandler({'https':os.environ['HTTPS_PROXY']})\n"
        "try:\n"
        " urllib.request.build_opener(proxy).open('https://127.0.0.1/',timeout=3)\n"
        " raise AssertionError('private proxy target')\n"
        "except OSError:pass\n"
        "pathlib.Path('legacy-result.txt').write_text('ok')\nprint('M2_LEGACY_PASS')\n"
    )
    if persistent:
        manager = DockerSandboxContainerManager(default_image=synthetic_image)
        container = manager.start(
            DockerSandboxContainerRequest(workspace=handle, network_enabled=True)
        )
        try:
            completed = docker("exec", container.container_name, "python", "-c", script)
            assert "M2_LEGACY_PASS" in completed.stdout
        finally:
            manager.stop(container)
    else:
        result = DockerSandboxRunner(default_image=synthetic_image).run(
            DockerSandboxCommand(
                workspace=handle, command=["python", "-c", script], network_enabled=True
            )
        )
        assert result.exit_code == 0, result.stderr
        assert "M2_LEGACY_PASS" in result.stdout
    assert (handle.repo_path / "legacy-result.txt").read_text() == "ok"


def test_allowlisted_local_source_is_immutable(tmp_path, synthetic_image, monkeypatch):
    handle = workspace(tmp_path)
    source = tmp_path / "allowlisted-source"
    source.mkdir()
    canary = source / "canary"
    canary.write_text("synthetic-source")
    canary.chmod(0o666)  # A write must fail because of the mount, not file ownership.
    source.chmod(0o777)
    handle.repo_url = source.as_uri()
    monkeypatch.setenv("CODE_AGENT_ALLOWED_LOCAL_REMOTES", str(source))
    manager = DockerSandboxContainerManager(default_image=synthetic_image)
    container = manager.start(
        DockerSandboxContainerRequest(workspace=handle, scratch_namespace="m2-source-check")
    )
    try:
        script = (
            f"from pathlib import Path\np=Path({str(canary)!r})\n"
            "assert p.read_text()=='synthetic-source'\n"
            "try:\n p.write_text('tampered');raise AssertionError('source is writable')\n"
            "except OSError:pass\n"
        )
        docker("exec", container.container_name, "python", "-c", script)
    finally:
        manager.stop(container)
    assert canary.read_text() == "synthetic-source"
