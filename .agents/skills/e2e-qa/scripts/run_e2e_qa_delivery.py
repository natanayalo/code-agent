import asyncio
import os
import shutil
import subprocess
from typing import Final

import httpx

# Configuration
API_URL: Final[str] = os.environ.get("CODE_AGENT_API_URL", "http://127.0.0.1:8000")
SHARED_SECRET = os.environ.get("CODE_AGENT_API_SHARED_SECRET", "ayalo123")


# Read workspace root from .env to match the docker compose volume mapping
DEFAULT_WORKSPACE_ROOT: Final[str] = os.path.expanduser("~/.code-agent/workspaces")

workspace_root = os.environ.get("CODE_AGENT_WORKSPACE_ROOT")
if not workspace_root:
    workspace_root = DEFAULT_WORKSPACE_ROOT
    if os.path.exists(".env"):
        with open(".env", encoding="utf-8") as f:
            for line in f:
                parts = line.split("=", 1)
                if len(parts) == 2 and parts[0].strip() == "CODE_AGENT_WORKSPACE_ROOT":
                    raw_val = parts[1].split('#', 1)[0].strip()
                    workspace_root = raw_val.strip("'").strip('"')
                    break
workspace_root = os.path.expandvars(os.path.expanduser(workspace_root))

DUMMY_REPO_DIR = os.path.join(workspace_root, "dummy_repo")
DUMMY_REMOTE_DIR = os.path.join(workspace_root, "dummy_remote.git")


def setup_dummy_repo():
    print(f"[*] Setting up dummy remote at {DUMMY_REMOTE_DIR}")
    shutil.rmtree(DUMMY_REMOTE_DIR, ignore_errors=True)
    os.makedirs(DUMMY_REMOTE_DIR, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=master"], cwd=DUMMY_REMOTE_DIR, check=True, capture_output=True
    )

    print(f"[*] Setting up dummy repository at {DUMMY_REPO_DIR}")
    shutil.rmtree(DUMMY_REPO_DIR, ignore_errors=True)
    os.makedirs(DUMMY_REPO_DIR, exist_ok=True)
    subprocess.run(
        ["git", "init"], cwd=DUMMY_REPO_DIR, check=True, capture_output=True
    )
    readme_path = os.path.join(DUMMY_REPO_DIR, "README.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write("# Dummy QA Repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=DUMMY_REPO_DIR, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "QA User"], cwd=DUMMY_REPO_DIR, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "qa@example.com"], cwd=DUMMY_REPO_DIR, check=True, capture_output=True
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
    subprocess.run(
        ["git", "remote", "add", "origin", f"file://{DUMMY_REMOTE_DIR}"], cwd=DUMMY_REPO_DIR, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "master"], cwd=DUMMY_REPO_DIR, check=True, capture_output=True
    )


async def main():
    print("=== Starting E2E Docker QA Automation for Delivery Node ===")
    setup_dummy_repo()

    print("[*] Waiting for API to become healthy...")
    async with httpx.AsyncClient(timeout=30.0) as client:
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
            "Create a file named qa-hello.txt containing the text 'Hello QA Delivery' and commit it."
        ),
        "repo_url": f"file://{DUMMY_REMOTE_DIR}",
        "branch": "master",
        "source": "qa",
        "worker_override": os.environ.get("CODE_AGENT_WORKER_OVERRIDE", "antigravity"),
        "constraints": {
            "delivery_mode": "branch",
            "delivery_branch": "qa/test-delivery"
        }
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
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
        max_attempts = 450  # Increased timeout since multiple agents (exec, verify, review, delivery) are running
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
                    print(f"    - Attempt {attempt + 1}/{max_attempts}: Transient status error {exc.response.status_code}: {exc}")
                    await asyncio.sleep(2)
                    continue
                raise

            print(f"    - Attempt {attempt + 1}/{max_attempts}: Status = {status}")

            if status in ["completed", "success", "failed", "cancelled", "error"]:
                print(f"\n[+] Task finished with status: {status}")
                print(f"    - Details: {task_data}")
                break

            await asyncio.sleep(2)
        else:
            raise RuntimeError("Task timed out.")

    print("\n[*] Verifying dummy remote repository for the delivered branch")
    
    # We check the bare repo for the new branch
    result = subprocess.run(
        ["git", "branch", "-a"], cwd=DUMMY_REMOTE_DIR, capture_output=True, text=True
    )
    if "qa/test-delivery" in result.stdout:
        print("  [+] Branch qa/test-delivery was successfully pushed to the remote!")
    else:
        print("  [-] Branch qa/test-delivery NOT FOUND in the remote.")
        print(f"  [-] Remote branches: {result.stdout}")
        
        # Check task timeline for delivery failed
        for event in task_data.get("timeline_events", []):
            print(f"  [*] Timeline event: {event.get('event_type')} - {event.get('message')}")
        raise RuntimeError("Delivery validation failed.")

    print("[+] Done.")


if __name__ == "__main__":
    asyncio.run(main())
