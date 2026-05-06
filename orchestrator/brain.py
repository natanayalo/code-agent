"""Optional orchestrator-brain contracts for TaskSpec enrichment and routing."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from typing import Any, Protocol, cast

from pydantic import Field

from db.enums import WorkerRuntimeMode
from orchestrator.state import (
    OrchestratorModel,
    OrchestratorState,
    TaskDeliveryMode,
    TaskPlan,
    TaskRequest,
    TaskRiskLevel,
    TaskSpec,
    TaskSpecType,
    WorkerType,
)
from workers.base import Worker, WorkerProfile, WorkerRequest

DEFAULT_ROUTE_BRAIN_TIMEOUT_SECONDS = 45
DEFAULT_ROUTE_PLANNER_PROFILE = "gemini-native-planner"

_ROUTE_SYSTEM_PROMPT = """
You are an orchestrator routing assistant.

Task:
- Recommend exactly one best worker or one best worker profile for the current task.
- Consider retries, prior failures, task shape, and the available workers/profiles.
- Respect that suggestions are advisory and may be clamped by deterministic policy.

Output contract:
- Return exactly one JSON object, and no extra prose.
- JSON schema:
  {
    "suggested_worker": "codex" | "gemini" | "openrouter" | null,
    "suggested_profile": "<profile-name>" | null,
    "rationale": "<short reason>"
  }
- Set at least one of suggested_worker or suggested_profile.
""".strip()

_ROUTE_MAX_SUMMARY_PREVIEW_CHARS = 300


def _unwrap_markdown_json_fence(text: str) -> str:
    """Extract JSON payload from a fenced markdown block when present."""
    stripped = text.strip()
    if not stripped:
        return stripped
    fenced_matches = re.findall(
        r"```(?:json)?\s*(.*?)```",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced_matches:
        return fenced_matches[-1].strip()
    return stripped


def _coerce_worker_type(value: object) -> WorkerType | None:
    """Normalize string worker values to the supported vocabulary."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"codex", "gemini", "openrouter"}:
        return cast(WorkerType, normalized)
    return None


class TaskSpecBrainSuggestion(OrchestratorModel):
    """Structured suggestion payload returned by an optional orchestrator brain."""

    assumptions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    suggested_risk_level: TaskRiskLevel | None = None
    suggested_task_type: TaskSpecType | None = None
    suggested_delivery_mode: TaskDeliveryMode | None = None
    rationale: str | None = None


class TaskSpecBrainMergeReport(OrchestratorModel):
    """Audit details about how brain suggestions were applied or ignored."""

    enabled: bool = True
    provider: str | None = None
    applied: bool = False
    added_assumptions: list[str] = Field(default_factory=list)
    added_acceptance_criteria: list[str] = Field(default_factory=list)
    added_non_goals: list[str] = Field(default_factory=list)
    added_clarification_questions: list[str] = Field(default_factory=list)
    added_verification_commands: list[str] = Field(default_factory=list)
    ignored_fields: list[str] = Field(default_factory=list)
    rationale: str | None = None
    error: str | None = None


class RouteBrainSuggestion(OrchestratorModel):
    """Structured route recommendation returned by an optional orchestrator brain."""

    suggested_worker: WorkerType | None = None
    suggested_profile: str | None = None
    rationale: str | None = None


class RouteBrainMergeReport(OrchestratorModel):
    """Audit details about how brain route suggestions were applied or ignored."""

    enabled: bool = True
    provider: str | None = None
    applied: bool = False
    suggested_worker: WorkerType | None = None
    suggested_profile: str | None = None
    ignored_fields: list[str] = Field(default_factory=list)
    rationale: str | None = None
    error: str | None = None
    final_chosen_worker: WorkerType | None = None
    final_chosen_profile: str | None = None
    final_runtime_mode: WorkerRuntimeMode | None = None
    final_route_reason: str | None = None


class OrchestratorBrain(Protocol):
    """Optional suggestion provider used by orchestrator nodes."""

    def suggest_task_spec(
        self,
        *,
        task: TaskRequest,
        task_kind: str | None,
        task_plan: TaskPlan | None,
        task_spec: TaskSpec,
    ) -> TaskSpecBrainSuggestion | None:
        """Return structured TaskSpec suggestions, or None when no change is needed."""

    async def suggest_route(
        self,
        *,
        state: OrchestratorState,
        available_workers: frozenset[str],
        available_profiles: Mapping[str, WorkerProfile] | None = None,
    ) -> RouteBrainSuggestion | None:
        """Return structured route suggestions, or None when no suggestion is needed."""


class RuleBasedOrchestratorBrain:
    """Small deterministic brain used as a safe bootstrap for optional enrichment."""

    def __init__(
        self,
        *,
        planner_worker: Worker | None = None,
        planner_profile: str = DEFAULT_ROUTE_PLANNER_PROFILE,
        planner_timeout_seconds: int = DEFAULT_ROUTE_BRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self.planner_worker = planner_worker
        self.planner_profile = planner_profile.strip() or DEFAULT_ROUTE_PLANNER_PROFILE
        self.planner_timeout_seconds = (
            planner_timeout_seconds
            if planner_timeout_seconds > 0
            else DEFAULT_ROUTE_BRAIN_TIMEOUT_SECONDS
        )

    def suggest_task_spec(
        self,
        *,
        task: TaskRequest,
        task_kind: str | None,
        task_plan: TaskPlan | None,
        task_spec: TaskSpec,
    ) -> TaskSpecBrainSuggestion | None:
        del task_kind, task_plan

        suggestion = TaskSpecBrainSuggestion(
            rationale="rule_based_task_spec_enrichment_v1",
        )
        normalized_text = task.task_text.lower()

        if task_spec.task_type == "investigation":
            suggestion.acceptance_criteria.append(
                "Summarize root cause findings and include the next recommended action."
            )
        if task_spec.delivery_mode == "draft_pr":
            suggestion.non_goals.append("Do not merge or deploy changes automatically.")
        if "urgent" in normalized_text and task_spec.risk_level == "low":
            suggestion.suggested_risk_level = "medium"

        if (
            not suggestion.assumptions
            and not suggestion.acceptance_criteria
            and not suggestion.non_goals
            and not suggestion.clarification_questions
            and not suggestion.verification_commands
            and suggestion.suggested_risk_level is None
            and suggestion.suggested_task_type is None
            and suggestion.suggested_delivery_mode is None
        ):
            return None
        return suggestion

    async def suggest_route(
        self,
        *,
        state: OrchestratorState,
        available_workers: frozenset[str],
        available_profiles: Mapping[str, WorkerProfile] | None = None,
    ) -> RouteBrainSuggestion | None:
        """Use the planner worker to produce a structured route recommendation."""
        if self.planner_worker is None:
            raise RuntimeError("planner route recommendation unavailable: planner worker not wired")

        profiles_payload: dict[str, Any] = {}
        if available_profiles:
            for name, profile in available_profiles.items():
                profiles_payload[name] = {
                    "worker_type": profile.worker_type,
                    "runtime_mode": profile.runtime_mode,
                    "mutation_policy": profile.mutation_policy,
                    "supported_delivery_modes": list(profile.supported_delivery_modes),
                    "capability_tags": list(profile.capability_tags),
                }

        task_text = state.normalized_task_text or state.task.task_text
        prompt_payload = {
            "task_text": task_text,
            "task_kind": state.task_kind,
            "attempt_count": state.attempt_count,
            "dispatch_worker": state.dispatch.worker_type,
            "verification_status": state.verification.status if state.verification else None,
            "verification_failure_kind": (
                state.verification.failure_kind if state.verification else None
            ),
            "result_status": state.result.status if state.result else None,
            "result_failure_kind": state.result.failure_kind if state.result else None,
            "available_workers": sorted(available_workers),
            "available_profiles": profiles_payload,
            "task_constraints": dict(state.task.constraints),
            "task_budget": dict(state.task.budget),
        }
        prompt = (
            "Return the best route recommendation for this orchestration context.\n\n"
            "Context JSON:\n"
            f"{json.dumps(prompt_payload, sort_keys=True)}\n"
        )

        constraints = dict(state.task.constraints)
        constraints["read_only"] = True
        constraints.pop("granted_permission", None)
        budget = dict(state.task.budget)
        budget["worker_timeout_seconds"] = self.planner_timeout_seconds

        request = WorkerRequest(
            session_id=state.session.session_id if state.session is not None else None,
            repo_url=state.task.repo_url,
            branch=state.task.branch,
            task_text=prompt,
            memory_context=state.memory.model_dump(),
            task_plan=state.task_plan.model_dump(mode="json") if state.task_plan else None,
            task_spec=state.task_spec.model_dump(mode="json") if state.task_spec else None,
            constraints=constraints,
            budget=budget,
            secrets=dict(state.task.secrets),
            tools=state.task.tools,
            worker_profile=self.planner_profile,
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        )

        try:
            result = await asyncio.wait_for(
                self.planner_worker.run(request, system_prompt=_ROUTE_SYSTEM_PROMPT),
                timeout=self.planner_timeout_seconds,
            )
        except TimeoutError as exc:  # pragma: no cover - covered via tests with stubs
            raise RuntimeError(
                f"planner route recommendation timed out after {self.planner_timeout_seconds}s"
            ) from exc
        except asyncio.CancelledError:  # pragma: no cover - passthrough
            raise
        except Exception as exc:  # pragma: no cover - covered via tests with stubs
            raise RuntimeError(
                "planner route recommendation failed: "
                f"{type(exc).__name__}: {str(exc).strip() or 'no detail'}"
            ) from exc

        if result.status != "success":
            preview = (result.summary or "no summary").strip().replace("\n", " ")
            if len(preview) > _ROUTE_MAX_SUMMARY_PREVIEW_CHARS:
                preview = preview[:_ROUTE_MAX_SUMMARY_PREVIEW_CHARS] + "..."
            raise RuntimeError(
                "planner route recommendation returned non-success status "
                f"'{result.status}': {preview}"
            )

        raw_summary = (result.summary or "").strip()
        if not raw_summary:
            raise RuntimeError("planner route recommendation returned an empty summary")

        normalized_json = _unwrap_markdown_json_fence(raw_summary)
        try:
            payload = json.loads(normalized_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "planner route recommendation returned invalid JSON: "
                f"{str(exc).strip() or 'parse error'}"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError("planner route recommendation returned a non-object JSON payload")

        raw_suggested_profile = payload.get("suggested_profile")
        suggested_profile = (
            raw_suggested_profile.strip()
            if isinstance(raw_suggested_profile, str) and raw_suggested_profile.strip()
            else None
        )
        raw_rationale = payload.get("rationale")
        rationale = (
            raw_rationale.strip()
            if isinstance(raw_rationale, str) and raw_rationale.strip()
            else None
        )

        suggestion = RouteBrainSuggestion(
            suggested_worker=_coerce_worker_type(payload.get("suggested_worker")),
            suggested_profile=suggested_profile,
            rationale=rationale,
        )
        if suggestion.suggested_worker is None and suggestion.suggested_profile is None:
            raise RuntimeError(
                "planner route recommendation omitted both suggested_worker and suggested_profile"
            )
        return suggestion
