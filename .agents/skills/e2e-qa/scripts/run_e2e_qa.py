import asyncio
import os
import shutil
import subprocess
from typing import Final
from urllib.parse import quote

import httpx


def _clean_env_value(raw_value: str) -> str | None:
    """Normalize a simple dotenv value without logging secret contents."""
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    else:
        value = value.split("#", 1)[0].strip()
    return value or None


def _read_env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value.strip()
    if not os.path.exists(".env"):
        return None
    with open(".env", encoding="utf-8") as f:
        for line in f:
            key, sep, raw_value = line.partition("=")
            if sep and key.strip() == name:
                return _clean_env_value(raw_value)
    return None


def _required_env_value(name: str) -> str:
    value = _read_env_value(name)
    if not value:
        raise RuntimeError(f"{name} must be set in the environment or .env.")
    return value


# Configuration
API_URL: Final[str] = os.environ.get("CODE_AGENT_API_URL", "http://127.0.0.1:8000")
SHARED_SECRET = _required_env_value("CODE_AGENT_API_SHARED_SECRET")
QA_REPO_KEY: Final[str] = os.environ.get("CODE_AGENT_QA_REPO_KEY", "qa-dummy")
PHOENIX_URL: Final[str] = os.environ.get("CODE_AGENT_PHOENIX_URL", "http://127.0.0.1:6006")
TRACING_PROJECT: Final[str] = os.environ.get(
    "CODE_AGENT_TRACING_PROJECT",
    _read_env_value("CODE_AGENT_TRACING_PROJECT") or "code-agent-local",
)
TRACE_EXPORT_WAIT_SECONDS: Final[int] = int(
    os.environ.get("CODE_AGENT_QA_TRACE_EXPORT_WAIT_SECONDS", "20")
)
ARTIFACT_VERIFY_COMMAND: Final[str] = (
    'python3 -c "from pathlib import Path; '
    "assert Path('qa-hello.txt').read_text().strip() == 'Hello QA'\""
)


# Read workspace root from .env to match the docker compose volume mapping
DEFAULT_WORKSPACE_ROOT: Final[str] = os.path.expanduser("~/.code-agent/workspaces")

workspace_root = _read_env_value("CODE_AGENT_WORKSPACE_ROOT") or DEFAULT_WORKSPACE_ROOT
workspace_root = os.path.expandvars(os.path.expanduser(workspace_root))

DUMMY_REPO_DIR = os.path.join(workspace_root, "dummy_repo")


def _is_enabled(raw_value: str | None) -> bool:
    return (raw_value or "").strip().lower() in {"1", "true", "yes", "on"}


def setup_dummy_repo():
    print(f"[*] Setting up dummy repository at {DUMMY_REPO_DIR}")
    shutil.rmtree(DUMMY_REPO_DIR, ignore_errors=True)
    os.makedirs(DUMMY_REPO_DIR, exist_ok=True)
    subprocess.run(["git", "init"], cwd=DUMMY_REPO_DIR, check=True, capture_output=True)
    readme_path = os.path.join(DUMMY_REPO_DIR, "README.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write("# Dummy QA Repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=DUMMY_REPO_DIR, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "QA User"],
        cwd=DUMMY_REPO_DIR,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "qa@example.com"],
        cwd=DUMMY_REPO_DIR,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=DUMMY_REPO_DIR,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-M", "master"], cwd=DUMMY_REPO_DIR, check=True, capture_output=True
    )


async def main():
    print("=== Starting E2E Docker QA Automation ===")
    setup_dummy_repo()

    print("[*] Waiting for API to become healthy...")
    async with httpx.AsyncClient() as client:
        for _ in range(60):
            try:
                resp = await client.get(f"{API_URL}/health")
                if resp.status_code == 200:
                    break
            except httpx.RequestError:
                pass
            await asyncio.sleep(2)
        else:
            raise RuntimeError("API failed to become healthy")

    print("[+] API is healthy!")

    print("[*] Submitting task via webhook")
    payload = {
        "task_text": (
            "Create a file named qa-hello.txt containing the text 'Hello QA' and commit it."
        ),
        "repo_key": QA_REPO_KEY,
        "branch": "master",
        "source": "qa",
        "worker_override": os.environ.get("CODE_AGENT_WORKER_OVERRIDE", "antigravity"),
        "constraints": {
            "verification_commands": [ARTIFACT_VERIFY_COMMAND],
            "acceptance_criteria": [
                "qa-hello.txt exists.",
                "qa-hello.txt contains exactly Hello QA.",
            ],
        },
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{API_URL}/webhook", json=payload, headers={"X-Webhook-Token": SHARED_SECRET}
        )
        resp.raise_for_status()
        data = resp.json()
        task_id = data.get("task_id")
        if not task_id:
            raise RuntimeError(f"Webhook response did not contain task_id: {data}")
        print(f"[+] Task ingested successfully. Task ID: {task_id}")

        print("[*] Polling for task completion...")
        max_attempts = 150  # Antigravity uses an LLM so it might take ~1-2 min
        for attempt in range(max_attempts):
            try:
                resp = await client.get(
                    f"{API_URL}/tasks/{task_id}", headers={"X-Webhook-Token": SHARED_SECRET}
                )
                resp.raise_for_status()
                task_data = resp.json()
                status = task_data.get("status")
            except httpx.RequestError as exc:
                print(f"    - Attempt {attempt + 1}/{max_attempts}: Request error: {exc}")
                await asyncio.sleep(2)
                continue
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {502, 503, 504}:
                    print(
                        f"    - Attempt {attempt + 1}/{max_attempts}: Transient status error "
                        f"{exc.response.status_code}: {exc}"
                    )
                    await asyncio.sleep(2)
                    continue
                raise

            print(f"    - Attempt {attempt + 1}/{max_attempts}: Status = {status}")

            if status in ["completed", "success", "failed", "cancelled", "error"]:
                print(f"\n[+] Task finished with status: {status}")
                print(f"    - Details: {task_data}")
                if status not in ["completed", "success"]:
                    raise RuntimeError(f"Task failed with status: {status}")
                break

            await asyncio.sleep(2)
        else:
            raise RuntimeError("Task timed out.")

        if _is_enabled(_read_env_value("CODE_AGENT_ENABLE_TRACING")):
            await wait_for_span_export(
                client,
                task_id=task_id,
                span_name="orchestrator.memory.observation_bridge",
            )

    print("\n[*] Verifying dummy repository artifacts")

    workspace_dir = None

    # We use the exact workspace ID returned by the API
    latest_run = task_data.get("latest_run")
    expected_workspace_id = latest_run.get("workspace_id") if latest_run else None
    if not expected_workspace_id:
        raise RuntimeError("Could not determine workspace_id from task_data")

    full_path = os.path.join(workspace_root, expected_workspace_id)
    if os.path.isdir(full_path):
        workspace_dir = full_path
    if workspace_dir:
        print(f"  [+] Found workspace: {workspace_dir}")
        check_file = os.path.join(workspace_dir, "qa-hello.txt")
        if os.path.exists(check_file):
            print("  [+] qa-hello.txt exists in workspace!")
            with open(check_file, encoding="utf-8") as f:
                content = f.read().strip()
                print(f"  [+] Content: {content}")
            if content != "Hello QA":
                raise RuntimeError(f"qa-hello.txt content mismatch: {content!r}")
        else:
            raise RuntimeError("qa-hello.txt NOT FOUND in workspace.")
    else:
        raise RuntimeError(f"No workspace directory found at {full_path}")

    print("[+] Done.")


async def wait_for_span_export(
    client: httpx.AsyncClient,
    *,
    task_id: str,
    span_name: str,
) -> None:
    print(f"[*] Waiting for Phoenix span export: {span_name}")
    spans_url = f"{PHOENIX_URL.rstrip('/')}/v1/projects/{quote(TRACING_PROJECT)}/spans"
    attempts = max(1, TRACE_EXPORT_WAIT_SECONDS // 2)
    last_error = None

    for attempt in range(attempts):
        try:
            resp = await client.get(
                spans_url,
                params={
                    "limit": "100",
                    "name": span_name,
                    "attribute": f"code_agent.task_id:{task_id}",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("data") if isinstance(data, dict) else None
            if rows:
                print(f"[+] Phoenix span found: {span_name}")
                return
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc

        print(f"    - Span attempt {attempt + 1}/{attempts}: not visible yet")
        await asyncio.sleep(2)

    raise RuntimeError(
        f"Phoenix span {span_name!r} was not visible for task {task_id} "
        f"after {TRACE_EXPORT_WAIT_SECONDS}s. Last error: {last_error}"
    )


if __name__ == "__main__":
    asyncio.run(main())
