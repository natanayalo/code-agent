---
name: webhooks
description: Documentation for using the generic webhook ingress to submit tasks and track progress.
---

# Webhooks Skill

Use this skill when you need to interact with the system via the generic webhook ingress (`POST /webhook`).

## 1. Endpoint Details

- **Endpoint**: `POST /webhook`
- **Auth Header**: `X-Webhook-Token: <CODE_AGENT_API_SHARED_SECRET>`
- **Content-Type**: `application/json`

## 2. Submit a Task

```bash
# Example submission
curl -sS -X POST "http://127.0.0.1:8000/webhook" \
  -H "X-Webhook-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "task_text": "List top-level files.",
    "repo_key": "project",
    "branch": "master",
    "source": "manual"
  }'
```

Replace project with a key already registered in CODE_AGENT_ALLOWED_REPOS.
The response will contain a task_id. Inspect the current payload model in
apps/api/routes/webhook.py when a field is unclear.

## 3. Tracking Progress

Poll the status of the task using the `/tasks/{task_id}` endpoint:

```bash
curl -sS -X GET "http://127.0.0.1:8000/tasks/$TASK_ID" \
  -H "X-Webhook-Token: $TOKEN"
```

## 4. Payload Schema

### Required Fields
- `task_text`: The natural language description of the task.

### Common Optional Fields
- `repo_key`: Key registered in CODE_AGENT_ALLOWED_REPOS; raw repo_url is not a supported ingress field.
- `branch`: Git branch to checkout.
- `source`: Label for the ingress source (defaults to `webhook`).
- `external_user_id`: Caller's unique identity.
- `external_thread_id`: Caller's thread/conversation identity.
- `delivery_id`: Idempotency key.
- `callback_url`: Public HTTP(S) URL for status callbacks.

## 5. Ingress Semantics

- **Session Channel**: Namespaced as `webhook:<source>`.
- **User Identity**: Namespaced as `webhook:<source>:<external_user_id>` (or `anonymous` if missing).
- **Default Repo**: Fallback to `CODE_AGENT_WEBHOOK_DEFAULT_REPO_KEY` if `repo_key` is not provided.
- **SSRF Protection**: `callback_url` must be a public, non-reserved IP/domain.
