# Codex Native Sandbox Policy

This document describes the runtime policy for Codex native agent execution, specifically regarding sandbox boundaries and Docker container alignment.

## Sandbox Modes

Codex `exec` supports several sandbox modes. Our system maps these modes based on the environment and repository trust:

1.  **`read-only`**: Used when the task constraints specify `read_only: true`. No modifications are allowed to the workspace.
2.  **`workspace-write`**: The default mode for untrusted repositories or when running outside a Docker container. It uses Codex's internal Linux namespace-based sandbox to restrict access.
3.  **`danger-full-access`**: A permissive mode that disables Codex's internal sandbox. This is used **ONLY** when:
    *   The worker process is running inside a Docker container (detected via `is_in_container()`).
    *   The repository is explicitly trusted via operator-controlled regex patterns.

## Trust Configuration

Repository trust is controlled by the operator via the following environment variable:

*   **`CODE_AGENT_CODEX_TRUSTED_REPO_PATTERNS`**: A comma-separated list of regex patterns. If the `repo_url` of a task matches any of these patterns, the repository is considered trusted.

Example:
```bash
CODE_AGENT_CODEX_TRUSTED_REPO_PATTERNS=".*github.com/my-org/.*,.*gitlab.com/trusted-project/.*"
```

## Security Guardrails

To prevent privilege escalation and ensure safe execution, the following guardrails are enforced:

*   **Docker as the Primary Boundary**: `danger-full-access` is only allowed when running inside a Docker container. This ensures that even if Codex's internal sandbox is disabled, the process is still bounded by the Docker container's isolation.
*   **No User-Controlled Trust**: Trust is NOT driven by task constraints or request payloads. It must be explicitly configured by the operator in the worker environment.
*   **Auditability**: Every native run records its sandbox decision inputs and final mode in the `budget_usage` metadata and logs.

## Decision Logic

The `CodexCliWorker` uses the following logic to select the sandbox mode:

```python
if read_only_requested:
    sandbox_mode = "read-only"
elif in_container and repo_trusted:
    sandbox_mode = "danger-full-access"
else:
    sandbox_mode = "workspace-write"
```
