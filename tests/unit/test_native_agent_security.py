from tools import ToolPermissionLevel
from workers.base import WorkerRequest
from workers.native_agent_security import native_github_credentials


def _request(*, tools: list[str], permission: str) -> WorkerRequest:
    return WorkerRequest(
        task_text="native task",
        repo_url="https://example.test/repo.git",
        tools=tools,
        constraints={"granted_permission": permission},
        secrets={"GH_TOKEN": "grant-token", "GITHUB_TOKEN": "grant-token"},
    )


def test_github_credentials_need_tool_and_explicit_write_grant() -> None:
    denied = _request(tools=["execute_github"], permission=ToolPermissionLevel.WORKSPACE_WRITE)
    absent_tool = _request(tools=["execute_git"], permission=ToolPermissionLevel.NETWORKED_WRITE)
    allowed = _request(tools=["execute_github"], permission=ToolPermissionLevel.GIT_PUSH_OR_DEPLOY)

    assert native_github_credentials(denied) == {}
    assert native_github_credentials(absent_tool) == {}
    assert native_github_credentials(allowed) == {
        "GH_TOKEN": "grant-token",
        "GITHUB_TOKEN": "grant-token",
    }
