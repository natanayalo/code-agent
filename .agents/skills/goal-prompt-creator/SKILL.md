---
name: goal-prompt-creator
description: Use when the user wants to decide whether a task is a good fit for a durable goal prompt and to draft a strong prompt with clear scope, success criteria, constraints, and verification.
---

# Goal Prompt Creator

Use this skill to turn an idea or task into a durable goal prompt only when that task is a good fit for a long-running agent workflow.

## Read first

- `AGENTS.md`
- this skill's [reference guide](references/goal_prompt_guide.md)

## Workflow

1. Ask a small set of clarifying questions before writing the prompt.
2. Determine whether the task is actually a good goal candidate.
3. If it is not a good fit, say so plainly and recommend the better prompt type.
4. If it is a good fit, write the goal prompt using the best-practice structure in the reference guide.

## Clarifying questions

Ask only what is needed to judge fit and write a strong goal prompt:

- What outcome do you want, in one sentence?
- What would success look like, and how will we verify it?
- What is the scope boundary or non-goal?
- What constraints matter most, such as time, budget, risk, or tools?
- Is this a long-running, multi-step task that benefits from durable state?

## Goal-fit check

Recommend a durable goal prompt when the task is:

- multi-step or iterative
- best handled by a durable objective rather than a one-shot answer
- verifiable with concrete checks
- likely to benefit from progress tracking, retries, or exploration

Do not recommend a durable goal prompt when the task is:

- a simple question or small one-off edit
- mostly subjective with no clear success criteria
- missing key context that would make progress unsafe or low-value
- better handled as a normal prompt, a plan, or a narrow subtask

## Output shape

If the task is not a good goal, return:

1. why it is not a strong goal candidate
2. the better alternative
3. the minimum extra information needed, if any

If the task is a good goal, return:

1. a short fit assessment
2. the final goal prompt
3. any optional notes about what to confirm before running it

## Quality bar

- Prefer concise, concrete language.
- Make success criteria observable.
- Include explicit non-goals when they reduce ambiguity.
- Preserve the user's actual intent; do not inflate scope.
- Keep the prompt durable enough to survive a long run without drifting.
- Favor outcome-based wording over implementation micromanagement unless the user specifically wants a constrained execution path.
