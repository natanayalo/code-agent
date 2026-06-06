# Goal Prompt Guide

This guide distills practical patterns for turning a task into a strong durable goal prompt.

## When a goal prompt is a good fit

Use a durable goal prompt when the task needs:

- durable state across many iterations
- branching exploration or debugging
- verification loops
- a clear finish condition
- enough autonomy that a one-shot prompt is too brittle

Common good fits:

- bug hunting and regression hardening
- refactors that must preserve behavior
- test expansion and coverage improvement
- migrations with checkpoints
- repeated verification workflows
- multi-file or multi-system changes

## When a goal prompt is a poor fit

Prefer a normal prompt or a short plan when the task is:

- a small isolated edit
- a direct factual question
- purely creative or subjective without clear acceptance criteria
- blocked on missing requirements
- so underspecified that the model would have to guess dangerous details

## Best-practice questions to ask

Ask the minimum needed to shape the goal:

- What should be true when this is done?
- How will we know it worked?
- What should definitely not change?
- What is in scope vs. out of scope?
- What constraints matter most?
- What should the agent do when it finds ambiguity or a blocker?

## Best-practice goal structure

Use this order:

1. Objective
2. Context
3. Constraints
4. Success criteria
5. Non-goals
6. Verification
7. Stop conditions or handoff conditions

## Prompt template

```text
Objective:
<one sentence describing the durable outcome>

Context:
<brief project or task context the agent needs>

Scope:
<what is in scope>

Non-goals:
<what must not be changed or attempted>

Constraints:
<time, budget, tools, safety, compatibility, style, or rollout limits>

Success criteria:
<observable checks that prove the goal is complete>

Verification:
<tests, commands, manual checks, or evidence required before stopping>

If blocked:
<what to ask about, what to preserve, how to proceed safely>
```

## Surface wrappers

Use the same core prompt body everywhere. Only add a wrapper or prefix if the target tool explicitly requires one.

## Good prompt characteristics

- specific rather than vague
- measurable rather than aspirational
- bounded rather than open-ended
- verification-oriented rather than implementation-only
- explicit about risks and non-goals
- written so another agent could continue the task later

## Bad prompt characteristics

- “make it better”
- “fix all bugs”
- “improve coverage” without a target or scope
- “keep going until done” without success criteria
- missing constraints, blockers, or verification

## Recommended assistant behavior

When writing the final prompt:

- preserve the user's language when possible
- trim filler and repetition
- use bullets for long goals
- mention whether a goal prompt is actually the right surface
- if it is not, say so and propose the better alternative
