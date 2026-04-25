# T-128 Evaluation Artifact: Role-Native vs Flattened (OpenRouter Slice)

Date: 2026-04-25

## Scope

This artifact compares request-shaping behavior for the OpenRouter runtime adapter:

- Legacy flattened mode (`CODE_AGENT_OPENROUTER_ROLE_NATIVE_MESSAGES=false`)
- Role-native mode (`CODE_AGENT_OPENROUTER_ROLE_NATIVE_MESSAGES=true`)

Comparison is based on unit-level payload inspection and adapter invariants.

## Representative Scenario

Transcript sample:

- system: runtime context
- assistant: prior tool decision
- tool: `execute_bash` result payload

Expected output contract remains unchanged in both modes:

- response format: JSON object matching `CliRuntimeStep` schema
- parser behavior: unchanged (same fenced JSON handling, final wrapping, and error handling)

## Observed Differences

### Flattened mode

- Sends one `user` message containing protocol + worker system prompt + entire transcript text.
- Lower provider compatibility risk due to single string payload.
- Harder to reason about role boundaries.

### Role-native mode

- Sends structured message array:
  - `system`: adapter protocol instructions
  - `system` (optional): worker system prompt
  - transcript roles serialized from shared strategy
- `tool` transcript entries are currently encoded as tagged `user` messages for OpenAI-compatible transport safety.
- Improves semantic separation and adapter portability.

## Reliability Notes

- Default behavior remains flattened mode.
- Role-native mode is fully gated by env flag and can be disabled immediately.
- Existing response parsing and failure handling paths are unchanged.

## Decision

Proceed with staged rollout for OpenRouter only (flagged), then expand to Codex/Gemini adapters after parity checks on replay/frozen tasks.
