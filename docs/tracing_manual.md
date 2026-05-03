# Tracing Manual (Phoenix + OpenInference)

This manual describes simple, script-free tracing operations.

## 1) Enable Tracing

Set these environment variables (for local Docker, usually in `.env`):

```bash
CODE_AGENT_ENABLE_TRACING=1
CODE_AGENT_TRACING_PROJECT=code-agent-local
CODE_AGENT_TRACING_OTLP_ENDPOINT=http://phoenix:6006/v1/traces
```

Start Phoenix:

```bash
docker compose --profile observability up -d phoenix
```

UI:

- [http://localhost:6006](http://localhost:6006)

## 2) Submit a Read-Only Task

```bash
API_BASE="http://127.0.0.1:8000"
TOKEN="<your CODE_AGENT_API_SHARED_SECRET>"
PROJECT="${CODE_AGENT_TRACING_PROJECT:-code-agent-local}"
```

Submit through `/tasks`:

```bash
TASK_ID="$(
  curl -sS -X POST "$API_BASE/tasks" \
    -H "X-Webhook-Token: $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "task_text": "Read-only: list top-level files and summarize architecture in 3 bullets.",
      "repo_url": "https://github.com/natanayalo/code-agent",
      "branch": "task/t-150.5-observability-migration",
      "session": {
        "channel": "manual-trace",
        "external_user_id": "manual",
        "external_thread_id": "manual-trace-1"
      }
    }' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["task_id"])'
)"
echo "$TASK_ID"
```

Poll until terminal:

```bash
curl -sS -X GET "$API_BASE/tasks/$TASK_ID" \
  -H "X-Webhook-Token: $TOKEN"
```

## 3) Find Spans for the Task

```bash
curl -sS --get "http://127.0.0.1:6006/v1/projects/$PROJECT/spans" \
  --data-urlencode "limit=500" \
  --data-urlencode "attribute=code_agent.task_id:$TASK_ID"
```

Extract trace IDs:

```bash
curl -sS --get "http://127.0.0.1:6006/v1/projects/$PROJECT/spans" \
  --data-urlencode "limit=500" \
  --data-urlencode "attribute=code_agent.task_id:$TASK_ID" \
  | python -c 'import json,sys; d=json.load(sys.stdin).get("data") or []; print("\\n".join(sorted({(s.get("context") or {}).get("trace_id","") for s in d if (s.get("context") or {}).get("trace_id")})))'
```

## 4) Inspect One Trace

```bash
TRACE_ID="<trace_id>"
curl -sS --get "http://127.0.0.1:6006/v1/projects/$PROJECT/spans" \
  --data-urlencode "limit=1000" \
  --data-urlencode "trace_id=$TRACE_ID"
```

## 5) What to Validate

- A span named `orchestrator.graph.run` exists.
- A span named `LangGraph` exists under `orchestrator.graph.run`.
- A span named `dispatch_job` exists under `LangGraph`.

Root-span nuance:

- `/tasks` ingress often has `orchestrator.graph.run` as root.
- `/webhook` ingress may have `api.webhook` as root, with `orchestrator.graph.run` nested under it.

## 6) Troubleshooting

- Task stays `pending`: worker is not consuming queue.
- Task stays `in_progress`: inspect worker logs and budget/timeout settings.
- No spans for task:
  - verify tracing env vars are active in running containers,
  - verify Phoenix is reachable,
  - verify project name matches `CODE_AGENT_TRACING_PROJECT`.
- Multiple traces for one task: can be retry attempts; inspect `code_agent.attempt_count`.

## API Reference

For full endpoint details, see [phoenix_api_manual.md](phoenix_api_manual.md).
