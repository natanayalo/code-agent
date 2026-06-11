import asyncio
import os
import shutil
import subprocess
from typing import Final

import httpx

# Configuration
API_URL = "http://127.0.0.1:8000"
SHARED_SECRET = "ayalo123"


# Read workspace root from .env to match the docker compose volume mapping
DEFAULT_WORKSPACE_ROOT: Final[str] = os.path.expanduser("~/.code-agent/workspaces")

workspace_root = DEFAULT_WORKSPACE_ROOT
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            if line.startswith("CODE_AGENT_WORKSPACE_ROOT="):
                workspace_root = line.split("=")[1].strip()
                break

DUMMY_REPO_DIR = os.path.join(workspace_root, "dummy_repo")


def setup_dummy_repo():
    print(f"[*] Setting up dummy repository at {DUMMY_REPO_DIR}")
    shutil.rmtree(DUMMY_REPO_DIR, ignore_errors=True)
    os.makedirs(DUMMY_REPO_DIR, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "master"], cwd=DUMMY_REPO_DIR, check=True, capture_output=True
    )
    readme_path = os.path.join(DUMMY_REPO_DIR, "README.md")
    with open(readme_path, "w") as f:
        f.write("# Dummy QA Repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=DUMMY_REPO_DIR, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "QA User"], cwd=DUMMY_REPO_DIR, check=True)
    subprocess.run(
        ["git", "config", "user.email", "qa@example.com"], cwd=DUMMY_REPO_DIR, check=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=DUMMY_REPO_DIR,
        check=True,
        capture_output=True,
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
        "repo_url": f"file://{DUMMY_REPO_DIR}",
        "branch": "master",
        "source": "qa",
        "worker_override": "gemini",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{API_URL}/webhook", json=payload, headers={"X-Webhook-Token": SHARED_SECRET}
        )
        resp.raise_for_status()
        data = resp.json()
        task_id = data.get("task_id")
        print(f"[+] Task ingested successfully. Task ID: {task_id}")

        print("[*] Polling for task completion...")
        max_attempts = 150  # Gemini is an LLM so it might take ~1-2 min
        for attempt in range(max_attempts):
            resp = await client.get(
                f"{API_URL}/tasks/{task_id}", headers={"X-Webhook-Token": SHARED_SECRET}
            )
            resp.raise_for_status()
            task_data = resp.json()
            status = task_data.get("status")

            print(f"    - Attempt {attempt + 1}/{max_attempts}: Status = {status}")

            if status in ["completed", "success", "failed"]:
                print(f"\n[+] Task finished with status: {status}")
                print(f"    - Details: {task_data}")
                break

            await asyncio.sleep(2)
        else:
            raise RuntimeError("Task timed out.")

    print("\n[*] Verifying dummy repository artifacts")

    workspace_dir = None

    # We use the exact workspace ID returned by the API
    expected_workspace_id = task_data.get("latest_run", {}).get("workspace_id")
    if not expected_workspace_id:
        print("  [-] Could not determine workspace_id from task_data")
        return

    full_path = os.path.join(workspace_root, expected_workspace_id)
    if os.path.isdir(full_path):
        workspace_dir = full_path
    if workspace_dir:
        print(f"  [+] Found workspace: {workspace_dir}")
        check_file = os.path.join(workspace_dir, "qa-hello.txt")
        if os.path.exists(check_file):
            print("  [+] qa-hello.txt exists in workspace!")
            with open(check_file) as f:
                print(f"  [+] Content: {f.read().strip()}")
        else:
            print("  [-] qa-hello.txt NOT FOUND in workspace.")
    else:
        print("  [-] No workspace directory found.")

    print("[+] Done.")


if __name__ == "__main__":
    asyncio.run(main())
