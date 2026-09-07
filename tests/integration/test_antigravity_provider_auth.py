"""AGY provider bootstrap crosses the sandbox boundary as a private, disposable copy."""

import json
import stat
from pathlib import Path

import pytest

from sandbox.capability import CapabilityGrantFactory, FileSystemAccessPolicy, NetworkEgressPolicy
from sandbox.native_agent_executor import DockerNativeAgentExecutor, native_agent_home_for_request
from sandbox.provider_bootstrap import ProviderBootstrapLoader
from sandbox.provider_stager import ProviderCredentialStager
from sandbox.secrets import SecretRegistry, SecretResolver, SecretScope
from sandbox.trusted_context import TrustedSandboxExecutionContext
from tests.integration.test_native_agent_integration import _is_docker_available
from tests.unit.test_gemini_cli_worker import _make_workspace


def _auth_context(tmp_path):
    source = tmp_path / "provider" / "antigravity-cli" / "antigravity-oauth-token"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps({"token": {"access_token": "fake-access", "refresh_token": "fake-refresh"}})
    )
    bootstrap = ProviderBootstrapLoader.load_antigravity(source.parent.parent)
    registry = SecretRegistry(bootstrap.definitions)
    grant = CapabilityGrantFactory(registry).create_grant(
        network=NetworkEgressPolicy.DISABLED,
        filesystem=FileSystemAccessPolicy.READ_ONLY,
        allowed_secret_refs=bootstrap.ref_names,
        granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
    )
    resolver = SecretResolver(registry, file_store=bootstrap.file_store)
    return source, TrustedSandboxExecutionContext(
        grant=grant, task_id="agy-auth-test", provider_bootstrap=bootstrap, secret_resolver=resolver
    )


def test_agy_auth_copy_can_refresh_without_changing_source(tmp_path: Path):
    source, context = _auth_context(tmp_path)
    original = source.read_bytes()
    bootstrap = context.provider_bootstrap
    assert bootstrap is not None
    resolved = [
        context.secret_resolver.resolve_for_sandbox(ref, context.grant)
        for ref in bootstrap.ref_names
    ]
    task_home = tmp_path / "task-home"
    ProviderCredentialStager.stage(
        resolved, destination_by_ref=bootstrap.destination_by_ref, task_home=task_home
    )
    staged = task_home / bootstrap.destination_by_ref["antigravity_oauth_token"]
    assert not staged.is_symlink()
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert stat.S_IMODE(staged.parent.stat().st_mode) == 0o700
    assert staged.read_bytes() == original
    staged.write_text('{"token":{"access_token":"refreshed-fake"}}')
    assert source.read_bytes() == original
    assert not (task_home / ".gemini/oauth_creds.json").exists()


@pytest.mark.skipif(not _is_docker_available(), reason="Docker worker image unavailable")
def test_agy_auth_readable_and_refreshable_by_container_user(tmp_path: Path):
    source, context = _auth_context(tmp_path)
    original = source.read_bytes()
    workspace = _make_workspace(tmp_path)
    (workspace.workspace_path / ".code-agent").mkdir()
    execution = DockerNativeAgentExecutor().run(
        command=[
            "/usr/local/bin/python",
            "-c",
            (
                "import json,os,stat; from pathlib import Path; "
                "p=Path.home()/'.gemini/antigravity-cli/antigravity-oauth-token'; "
                "assert os.getuid()==65532; assert not p.is_symlink(); "
                "assert stat.S_IMODE(p.stat().st_mode)==0o600; "
                "assert json.loads(p.read_text())['token']['refresh_token']=='fake-refresh'; "
                "p.write_text('refreshed-fake'); print('provider-copy-ok')"
            ),
        ],
        prompt=None,
        workspace=workspace,
        artifact_root=tmp_path / "artifacts",
        environment={},
        timeout_seconds=30,
        scratch_namespace="agy-auth",
        cancel_requested=None,
        redactor=None,
        context=context,
    )
    assert execution.completed.returncode == 0, execution.completed.stderr
    assert "provider-copy-ok" in execution.completed.stdout
    assert source.read_bytes() == original
    assert not native_agent_home_for_request(workspace.workspace_path, "agy-auth").exists()
