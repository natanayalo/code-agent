from sandbox.native_agent_executor import is_public_egress_host
from tools import ToolPermissionLevel
from workers.base import WorkerRequest
from workers.native_agent_security import native_github_credentials


def _request(*, tools: list[str], permission: str) -> WorkerRequest:
    return WorkerRequest(
        task_text="native task",
        repo_url="https://example.test/repo.git",
        tools=tools,
        constraints={"granted_permission": permission},
        secrets={"GH_TOKEN": "real-gh-token", "GITHUB_TOKEN": "real-github-token"},
    )


def test_github_credentials_need_tool_and_explicit_write_grant() -> None:
    denied = _request(tools=["execute_github"], permission=ToolPermissionLevel.WORKSPACE_WRITE)
    absent_tool = _request(tools=["execute_git"], permission=ToolPermissionLevel.NETWORKED_WRITE)
    allowed = _request(tools=["execute_github"], permission=ToolPermissionLevel.GIT_PUSH_OR_DEPLOY)

    assert native_github_credentials(denied) == {}
    assert native_github_credentials(absent_tool) == {}
    assert native_github_credentials(allowed) == {
        "GH_TOKEN": "real-gh-token",
        "GITHUB_TOKEN": "real-github-token",
    }


def test_is_public_egress_host_blocks_private_and_non_global_ips() -> None:
    # Special hostnames
    assert not is_public_egress_host("localhost", ["93.184.216.34"])
    assert not is_public_egress_host("host.docker.internal", ["93.184.216.34"])
    assert not is_public_egress_host("metadata.google.internal", ["93.184.216.34"])
    assert not is_public_egress_host("", ["93.184.216.34"])

    # IPv4 non-global addresses
    assert not is_public_egress_host("metadata.local", ["169.254.169.254"])  # Link-local / AWS/GCP
    assert not is_public_egress_host("internal.local", ["10.0.0.1"])  # RFC1918
    assert not is_public_egress_host("internal.local", ["172.16.0.1"])  # RFC1918
    assert not is_public_egress_host("internal.local", ["192.168.1.1"])  # RFC1918
    assert not is_public_egress_host("loopback.local", ["127.0.0.1"])  # Loopback
    assert not is_public_egress_host("cgnat.local", ["100.64.0.1"])  # CGNAT

    # IPv6 non-global addresses
    assert not is_public_egress_host("loopback6.local", ["::1"])  # IPv6 loopback
    assert not is_public_egress_host("linklocal6.local", ["fe80::1"])  # IPv6 link-local
    assert not is_public_egress_host("ula6.local", ["fd00:ec2::254"])  # IPv6 ULA (AWS metadata)
    assert not is_public_egress_host("ula6.local", ["fc00::1"])  # IPv6 ULA (fc00::/7)

    # Valid public global addresses
    assert is_public_egress_host("example.com", ["93.184.216.34"])
    assert is_public_egress_host("example.com", ["2606:2800:220:1:248:1893:25c8:1946"])

    # Invalid addresses
    assert not is_public_egress_host("example.com", ["not-an-ip"])
    assert not is_public_egress_host("example.com", [])
