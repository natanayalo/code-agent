# Webhook Manual

This manual documents the simplest way to use the generic webhook ingress.

## Endpoint

- `POST /webhook`
- Auth header: `X-Webhook-Token: <CODE_AGENT_API_SHARED_SECRET>`

## Quick Start

```bash
API_BASE="http://127.0.0.1:8000"
TOKEN="<your CODE_AGENT_API_SHARED_SECRET>"
```

Submit a task:

```bash
curl -sS -X POST "$API_BASE/webhook" \
  -H "X-Webhook-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "task_text": "Read-only: list top-level files and summarize architecture in 3 bullets.",
    "repo_url": "https://github.com/natanayalo/code-agent",
    "branch": "task/t-150.5-observability-migration",
    "source": "manual"
  }'
```

Response includes `task_id`.

Poll task status:

```bash
TASK_ID="<task_id from submit response>"
curl -sS -X GET "$API_BASE/tasks/$TASK_ID" \
  -H "X-Webhook-Token: $TOKEN"
```

## Minimal Payload

Only `task_text` is required.

```json
{
  "task_text": "Read-only: explain repository structure."
}
```

## Common Optional Fields

- `repo_url`: repo to run against.
- `branch`: git branch to use.
- `source`: ingress source label. Defaults to `webhook`.
- `external_user_id`: caller identity.
- `external_thread_id`: conversation/thread identity.
- `display_name`: human-friendly caller name.
- `delivery_id`: idempotency key per channel/source.
- `constraints`: execution constraints.
- `budget`: runtime budget.
- `callback_url`: outbound status callback target.

## Ingress Semantics

- Session channel is namespaced as `webhook:<source>`.
- If `external_user_id` is provided, it is namespaced as `webhook:<source>:<external_user_id>`.
- If `external_user_id` is missing, it becomes `webhook:<source>:anonymous`.
- If `external_thread_id` is missing, a UUID is generated.
- If `repo_url` is missing, the server can fall back to `CODE_AGENT_WEBHOOK_DEFAULT_REPO_URL`.

## Callback URL Guardrails

`callback_url` must be public HTTP(S). Local/private/link-local/reserved targets are rejected by SSRF safety checks.
