# Role-Native Adapter Messaging Strategy (T-128)

## Goal

Migrate safely from flattened transcript prompts to role-native message arrays where provider/runtime support is reliable, while preserving rollback and deterministic behavior.

## Shared Message Shape

Runtime adapters should model transcript turns with this role set:

- `system`: stable protocol instructions or runtime context
- `user`: direct user/task instructions
- `assistant`: adapter/model prior decisions
- `tool`: observed tool output metadata + payload

The shared role-native shape is defined in `workers/adapter_messages.py`.

## Provider Serialization Policy

Some OpenAI-compatible endpoints do not reliably accept `tool` role messages without a `tool_call_id`.

Current policy:

- Keep `tool` in shared internal shape
- Serialize `tool` entries as tagged `user` messages for OpenAI-compatible chat payloads
- Preserve other roles as-is

This keeps transcript semantics explicit while avoiding provider-level schema rejection.

## Rollout Plan

1. Baseline mode remains default: flattened single-prompt payload.
2. Enable role-native mode per adapter using explicit env flag.
3. Compare outputs and request payload size/cost signals on representative tasks.
4. Promote adapter-by-adapter only after parity is acceptable.

## OpenRouter Slice

- Adapter: `workers/openrouter_adapter.py`
- Flag: `CODE_AGENT_OPENROUTER_ROLE_NATIVE_MESSAGES`
- Default: disabled (`false`)

When enabled, requests use role-native message arrays with:

- one system message containing protocol instructions plus optional worker prompt section
- serialized transcript entries

## Rollback

Set `CODE_AGENT_OPENROUTER_ROLE_NATIVE_MESSAGES=false` (or unset it) to restore legacy flattened prompt behavior immediately, without code changes.

## Follow-ups

- Add equivalent controlled flag paths to Codex/Gemini adapters.
- Add CI-level A/B replay checks against frozen-suite fixtures for role-native parity.
