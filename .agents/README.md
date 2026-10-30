# Agent procedures

[AGENTS.md](../AGENTS.md) is the canonical contributor policy. These skills add
procedure-specific details; they do not redefine permissions, test gates, or
product priorities. [Current status](../docs/status.md#current-backlog) selects
work; the [roadmap](../docs/roadmap.md) defines active and conditional scope.

## Skill index

| Skill | Use when |
| --- | --- |
| [start-task](skills/start-task/SKILL.md) | Starting a named task or selecting the next ready implementation slice. |
| [address-review](skills/address-review/SKILL.md) | Applying and verifying PR review feedback, with replies/resolution in its authorized mode. |
| [pr-reviewer](skills/pr-reviewer/SKILL.md) | Reviewing a PR or patch for actionable defects. |
| [db-schema](skills/db-schema/SKILL.md) | Changing SQLAlchemy models or Alembic migrations. |
| [dashboard](skills/dashboard/SKILL.md) | Developing the React/Vite dashboard. |
| [dashboard-qa](skills/dashboard-qa/SKILL.md) | Checking rendered dashboard layout and interactions. |
| [e2e-qa](skills/e2e-qa/SKILL.md) | Running the live webhook-to-worker happy path against disposable data. |
| [scout-e2e-qa](skills/scout-e2e-qa/SKILL.md) | Running the corresponding read-only scout flow. |
| [webhooks](skills/webhooks/SKILL.md) | Submitting and inspecting tasks through generic webhook ingress. |
| [tracing](skills/tracing/SKILL.md) | Inspecting Phoenix/OpenInference traces and execution artifacts. |
| [goal-prompt-creator](skills/goal-prompt-creator/SKILL.md) | Assessing and drafting a durable goal prompt. |

## Compatibility entrypoints

The files in `rules/` and `workflows/` remain short links for integrations that
already use those paths. Maintain executable procedures in the relevant skill,
policy in AGENTS.md, and current task state in docs/status.md. Completed task
specifications and evaluation records belong in docs/archive/ or evaluation/;
they are not instructions to repeat old work.
