---
name: tracing
description: Instructions for enabling, submitting, and querying distributed traces using Arize Phoenix and OpenInference.
---

# Tracing Skill

Use this skill when you need to enable tracing, submit tasks for observability validation, or query spans and traces from a Phoenix instance.

## 1. Enable Tracing

Ensure these environment variables are set (typically in `.env` or container environment):

```bash
CODE_AGENT_ENABLE_TRACING=1
CODE_AGENT_TRACING_PROJECT=code-agent-local
CODE_AGENT_TRACING_OTLP_ENDPOINT=http://phoenix:6006/v1/traces
```

Start the Phoenix container if it's not running:

```bash
docker compose --profile observability up -d phoenix
```

UI is available at: [http://localhost:6006](http://localhost:6006)

## 2. Submit a Task for Tracing

Use a read-only task to verify the tracing pipeline:

```bash
# Example submission using curl
curl -sS -X POST "http://127.0.0.1:8000/tasks" \
  -H "X-Webhook-Token: $CODE_AGENT_API_SHARED_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "task_text": "Read-only: list top-level files.",
    "repo_url": "https://github.com/natanayalo/code-agent",
    "branch": "master",
    "session": {
      "channel": "manual-trace",
      "external_user_id": "manual",
      "external_thread_id": "manual-trace-1"
    }
  }'
```

## 3. Query Spans and Traces

Query spans by `task_id` using the Phoenix REST API:

```bash
# Find all spans for a specific task
curl -sS --get "http://127.0.0.1:6006/v1/projects/$PROJECT/spans" \
  --data-urlencode "limit=500" \
  --data-urlencode "attribute=code_agent.task_id:$TASK_ID"
```

Extract trace IDs from the span list:

```bash
# Python helper to extract unique trace IDs
python -c 'import json,sys; d=json.load(sys.stdin).get("data") or []; print("\n".join(sorted({(s.get("context") or {}).get("trace_id","") for s in d if (s.get("context") or {}).get("trace_id")})))'
```

## 4. Query Sub-Spans Within a Trace

Use the Python client to fetch and explore the span hierarchy:

```python
from phoenix.client import Client
from collections import defaultdict

client = Client(base_url="http://localhost:6006")

# Get spans for a project
df = client.spans.get_spans_dataframe(project_identifier="PROJECT_ID", limit=10000)

# Filter by trace_id
trace_df = df[df["context.trace_id"] == "TRACE_ID"]

# Build parent-child hierarchy
children = defaultdict(list)
for _, row in trace_df.iterrows():
    children[row.get("parent_id")].append(row)

# Access spans by parent: children["parent_span_id"]
```

Key columns:
- `context.trace_id`: Trace identifier
- `context.span_id`: Span identifier
- `parent_id`: Reference to parent span
- `name`: Span name
- `start_time`: Span start timestamp

## 5. Troubleshooting

- **No Spans**: Verify `CODE_AGENT_TRACING_PROJECT` matches exactly and Phoenix is reachable from the worker container.
- **Multiple Traces**: Check `code_agent.attempt_count` attributes; retries will generate separate traces for the same task.
- **UI Empty**: Ensure OTLP endpoint is correct and spans are being exported (batch vs. immediate mode).

## 6. API Reference

For full Phoenix REST API documentation, see [phoenix_api.md](./resources/phoenix_api.md).
