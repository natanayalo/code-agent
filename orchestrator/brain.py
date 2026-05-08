"""Optional orchestrator-brain contracts for TaskSpec enrichment and routing."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any, Final, Literal, Protocol, cast

from pydantic import Field

from db.enums import WorkerRuntimeMode
from orchestrator.constants import RISK_ORDER
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

logger = logging.getLogger(__name__)

DEFAULT_ROUTE_BRAIN_TIMEOUT_SECONDS = 120
DEFAULT_TASK_SPEC_BRAIN_TIMEOUT_SECONDS = 90
DEFAULT_ROUTE_PLANNER_PROFILE = "gemini-native-planner"
DEFAULT_BRAIN_TIMEOUT_BUFFER_SECONDS: Final = 5

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
    "suggested_retry_strategy": "retry_same_worker" | "escalate_to_alternate" | null,
    "rationale": "<short reason>"
  }
- Set at least one of suggested_worker, suggested_profile, or suggested_retry_strategy.
""".strip()

_VERIFICATION_SYSTEM_PROMPT = """
You are an orchestrator verification-acceptance assistant.

Task:
- Review verification context and suggest whether warning-level outcomes may be accepted.
- Your suggestion is advisory only and will be clamped by deterministic policy.
- Never suggest suppressing hard failures.

Output contract:
- Return exactly one JSON object, and no extra prose.
- JSON schema:
  {
    "accept_warning_status": true | false | null,
    "rationale": "<short reason>"
  }
""".strip()

_TASK_SPEC_SYSTEM_PROMPT = """
You are an orchestrator enrichment assistant.

Task:
- Review the task text and the current deterministic task spec.
- Suggest additional assumptions, acceptance criteria, non-goals, clarification questions,
  and verification commands.
- You can also suggest a risk level, task type, and delivery mode, but these are subject
  to strict policy clamps.

Output contract:
- Return exactly one JSON object, and no extra prose.
- JSON schema:
  {
    "assumptions": ["<assumption>"],
    "acceptance_criteria": ["<criterion>"],
    "non_goals": ["<non-goal>"],
    "clarification_questions": ["<question>"],
    "verification_commands": ["<command>"],
    "suggested_risk_level": "low" | "medium" | "high" | null,
    "suggested_task_type": "feature" | "bug" | "refactor" | "investigation" | "docs" | null,
    "suggested_delivery_mode": "workspace" | "branch" | "draft_pr" | "summary" | null,
    "rationale": "<short reason>"
  }
""".strip()

_ROUTE_MAX_SUMMARY_PREVIEW_CHARS = 300
_RULES_RATIONALE = "rules_v1"


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


def _to_serializable(obj: Any) -> Any:
    """Recursively ensure Mapping types are dicts for robust JSON serialization."""
    if isinstance(obj, Mapping):
        return {str(k): _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        return [_to_serializable(item) for item in obj]
    if isinstance(obj, Enum):
        return obj.value
    return obj


def _merge_list(a: list[str], b: list[str]) -> list[str]:
    """Merge two lists of strings, preserving order and ensuring uniqueness."""
    return list(dict.fromkeys(a + b))


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
    suggested_retry_strategy: str | None = None
    rationale: str | None = None


class RouteBrainMergeReport(OrchestratorModel):
    """Audit details about how brain route suggestions were applied or ignored."""

    enabled: bool = True
    provider: str | None = None
    applied: bool = False
    suggested_worker: WorkerType | None = None
    suggested_profile: str | None = None
    suggested_retry_strategy: str | None = None
    ignored_fields: list[str] = Field(default_factory=list)
    rationale: str | None = None
    error: str | None = None
    final_chosen_worker: WorkerType | None = None
    final_chosen_profile: str | None = None
    final_runtime_mode: WorkerRuntimeMode | None = None
    final_route_reason: str | None = None


class VerificationBrainSuggestion(OrchestratorModel):
    """Structured verification acceptance recommendation from an optional brain."""

    accept_warning_status: bool | None = None
    rationale: str | None = None


class VerificationBrainMergeReport(OrchestratorModel):
    """Audit details about how verification brain suggestions were applied or ignored."""

    enabled: bool = True
    provider: str | None = None
    applied: bool = False
    accept_warning_status: bool | None = None
    ignored_fields: list[str] = Field(default_factory=list)
    rationale: str | None = None
    error: str | None = None
    final_verification_status: Literal["passed", "failed", "warning"] | None = None


class OrchestratorBrain(Protocol):
    """Optional suggestion provider used by orchestrator nodes."""

    async def suggest_task_spec(
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

    async def suggest_verification(
        self,
        *,
        state: OrchestratorState,
        independent_verifier_outcome: tuple[Literal["passed", "failed", "warning"], str]
        | None = None,
    ) -> VerificationBrainSuggestion | None:
        """Return structured verification-acceptance suggestions, or None."""


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

    async def suggest_task_spec(
        self,
        *,
        task: TaskRequest,
        task_kind: str | None,
        task_plan: TaskPlan | None,
        task_spec: TaskSpec,
    ) -> TaskSpecBrainSuggestion | None:
        # 1. Run rule-based bootstrap logic first
        suggestion = self._suggest_task_spec_rules(
            task=task,
            task_kind=task_kind,
            task_plan=task_plan,
            task_spec=task_spec,
        ) or TaskSpecBrainSuggestion(rationale="brain_enrichment_v1")

        # 2. Attempt model-backed enrichment if planner is available
        if self.planner_worker is not None:
            model_suggestion = await self._suggest_task_spec_model(
                task=task,
                task_kind=task_kind,
                task_plan=task_plan,
                task_spec=task_spec,
            )
            if model_suggestion:
                suggestion = self._merge_task_spec_suggestions(suggestion, model_suggestion)

        # If no enrichment fields were suggested (ignoring rationale), skip applying
        has_enrichment = any(
            getattr(suggestion, field)
            for field in type(suggestion).model_fields
            if field != "rationale"
        )
        if not has_enrichment:
            return None
        return suggestion

    def _suggest_task_spec_rules(
        self,
        *,
        task: TaskRequest,
        task_kind: str | None,
        task_plan: TaskPlan | None,
        task_spec: TaskSpec,
    ) -> TaskSpecBrainSuggestion | None:
        del task_kind, task_plan

        suggestion = TaskSpecBrainSuggestion(
            rationale=_RULES_RATIONALE,
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

    async def _suggest_task_spec_model(
        self,
        *,
        task: TaskRequest,
        task_kind: str | None,
        task_plan: TaskPlan | None,
        task_spec: TaskSpec,
    ) -> TaskSpecBrainSuggestion | None:
        """Use the planner worker to produce model-backed TaskSpec enrichment."""
        if self.planner_worker is None:
            return None

        prompt_payload = {
            "task_text": task.task_text,
            "task_kind": task_kind,
            "deterministic_spec": task_spec.model_dump(mode="json"),
            "task_plan": task_plan.model_dump(mode="json") if task_plan else None,
            "constraints": dict(task.constraints),
        }
        context_json = json.dumps(_to_serializable(prompt_payload), sort_keys=True, default=str)
        prompt = (
            "Suggest TaskSpec enrichments for this task.\n\n" "Context JSON:\n" f"{context_json}\n"
        )

        constraints = dict(task.constraints)
        constraints["read_only"] = True
        constraints.pop("granted_permission", None)
        budget = dict(task.budget)
        budget["worker_timeout_seconds"] = DEFAULT_TASK_SPEC_BRAIN_TIMEOUT_SECONDS

        request = WorkerRequest(
            session_id=None,  # Brain runs are detached from session state by default
            repo_url=task.repo_url,
            branch=task.branch,
            task_text=prompt,
            memory_context={},  # TaskSpec generation happens before memory loading
            task_plan=task_plan.model_dump(mode="json") if task_plan else None,
            task_spec=task_spec.model_dump(mode="json"),
            constraints=constraints,
            budget=budget,
            secrets={},
            tools=task.tools,
            worker_profile=self.planner_profile,
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        )

        try:
            async with asyncio.timeout(
                DEFAULT_TASK_SPEC_BRAIN_TIMEOUT_SECONDS + DEFAULT_BRAIN_TIMEOUT_BUFFER_SECONDS
            ):
                result = await self.planner_worker.run(
                    request, system_prompt=_TASK_SPEC_SYSTEM_PROMPT
                )
        except TimeoutError:
            logger.warning("planner task spec enrichment timed out; using deterministic defaults")
            return None
        except Exception as exc:
            logger.warning(
                f"planner task spec enrichment failed: {type(exc).__name__}: {exc}; "
                "using deterministic defaults"
            )
            return None

        if result.status != "success":
            logger.warning(
                f"planner task spec enrichment returned non-success status '{result.status}'"
            )
            return None

        raw_summary = (result.summary or "").strip()
        if not raw_summary:
            return None

        normalized_json = _unwrap_markdown_json_fence(raw_summary)
        try:
            data = json.loads(normalized_json)
            return TaskSpecBrainSuggestion.model_validate(data)
        except Exception as exc:
            logger.warning(f"Failed to parse brain task spec suggestion: {exc}")
            return None

    @staticmethod
    def _merge_task_spec_suggestions(
        base: TaskSpecBrainSuggestion,
        model: TaskSpecBrainSuggestion,
    ) -> TaskSpecBrainSuggestion:
        """Merge model-backed suggestions into rule-based ones."""
        return TaskSpecBrainSuggestion(
            assumptions=_merge_list(base.assumptions, model.assumptions),
            acceptance_criteria=_merge_list(base.acceptance_criteria, model.acceptance_criteria),
            non_goals=_merge_list(base.non_goals, model.non_goals),
            clarification_questions=_merge_list(
                base.clarification_questions, model.clarification_questions
            ),
            verification_commands=_merge_list(
                base.verification_commands, model.verification_commands
            ),
            suggested_risk_level=max(
                (r for r in [base.suggested_risk_level, model.suggested_risk_level] if r),
                key=lambda r: RISK_ORDER[cast(str, r)],
                default=None,
            ),
            suggested_task_type=model.suggested_task_type or base.suggested_task_type,
            suggested_delivery_mode=model.suggested_delivery_mode or base.suggested_delivery_mode,
            rationale=" | ".join(
                [
                    f"[{k}] {v}"
                    for k, v in [("rules", base.rationale), ("model", model.rationale)]
                    if v
                ]
            )
            or None,
        )

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
        context_json = json.dumps(_to_serializable(prompt_payload), sort_keys=True, default=str)
        prompt = (
            "Return the best route recommendation for this orchestration context.\n\n"
            "Context JSON:\n"
            f"{context_json}\n"
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
            secrets={},
            tools=state.task.tools,
            worker_profile=self.planner_profile,
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        )

        try:
            async with asyncio.timeout(
                self.planner_timeout_seconds + DEFAULT_BRAIN_TIMEOUT_BUFFER_SECONDS
            ):
                result = await self.planner_worker.run(request, system_prompt=_ROUTE_SYSTEM_PROMPT)
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
            suggested_retry_strategy=payload.get("suggested_retry_strategy"),
            rationale=rationale,
        )
        if (
            suggestion.suggested_worker is None
            and suggestion.suggested_profile is None
            and suggestion.suggested_retry_strategy is None
        ):
            raise RuntimeError(
                "planner route recommendation omitted worker/profile and retry strategy hints"
            )
        return suggestion

    async def suggest_verification(
        self,
        *,
        state: OrchestratorState,
        independent_verifier_outcome: tuple[Literal["passed", "failed", "warning"], str]
        | None = None,
    ) -> VerificationBrainSuggestion | None:
        """Use the planner worker to produce structured verification-acceptance hints."""
        if self.planner_worker is None:
            raise RuntimeError(
                "planner verification recommendation unavailable: planner worker not wired"
            )

        task_text = state.normalized_task_text or state.task.task_text
        worker_result_summary = state.result.summary if state.result else None
        if worker_result_summary and len(worker_result_summary) > 2000:
            worker_result_summary = worker_result_summary[:2000] + "... [truncated]"

        verification_context = {
            "task_text": task_text,
            "task_kind": state.task_kind,
            "attempt_count": state.attempt_count,
            "dispatch_worker": state.dispatch.worker_type,
            "worker_result_status": state.result.status if state.result else None,
            "worker_result_summary": worker_result_summary,
            "worker_failure_kind": state.result.failure_kind if state.result else None,
            "files_changed_count": len(state.result.files_changed) if state.result else 0,
            "failed_tests_count": len(
                [
                    test
                    for test in (state.result.test_results if state.result else [])
                    if test.status in ("failed", "error")
                ]
            ),
            "independent_verifier_outcome": (
                {
                    "status": independent_verifier_outcome[0],
                    "summary": independent_verifier_outcome[1],
                }
                if independent_verifier_outcome is not None
                else None
            ),
            "task_constraints": dict(state.task.constraints),
            "task_budget": dict(state.task.budget),
        }
        context_json = json.dumps(
            _to_serializable(verification_context),
            sort_keys=True,
            default=str,
        )
        prompt = (
            "Return verification acceptance guidance for this orchestration context.\n\n"
            "Context JSON:\n"
            f"{context_json}\n"
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
            secrets={},
            tools=state.task.tools,
            worker_profile=self.planner_profile,
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        )

        try:
            async with asyncio.timeout(
                self.planner_timeout_seconds + DEFAULT_BRAIN_TIMEOUT_BUFFER_SECONDS
            ):
                result = await self.planner_worker.run(
                    request,
                    system_prompt=_VERIFICATION_SYSTEM_PROMPT,
                )
        except TimeoutError as exc:  # pragma: no cover - covered via tests with stubs
            raise RuntimeError(
                "planner verification recommendation timed out after "
                f"{self.planner_timeout_seconds}s"
            ) from exc
        except asyncio.CancelledError:  # pragma: no cover - passthrough
            raise
        except Exception as exc:  # pragma: no cover - covered via tests with stubs
            raise RuntimeError(
                "planner verification recommendation failed: "
                f"{type(exc).__name__}: {str(exc).strip() or 'no detail'}"
            ) from exc

        if result.status != "success":
            preview = (result.summary or "no summary").strip().replace("\n", " ")
            if len(preview) > _ROUTE_MAX_SUMMARY_PREVIEW_CHARS:
                preview = preview[:_ROUTE_MAX_SUMMARY_PREVIEW_CHARS] + "..."
            raise RuntimeError(
                "planner verification recommendation returned non-success status "
                f"'{result.status}': {preview}"
            )

        raw_summary = (result.summary or "").strip()
        if not raw_summary:
            raise RuntimeError("planner verification recommendation returned an empty summary")

        normalized_json = _unwrap_markdown_json_fence(raw_summary)
        try:
            payload = json.loads(normalized_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "planner verification recommendation returned invalid JSON: "
                f"{str(exc).strip() or 'parse error'}"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError(
                "planner verification recommendation returned a non-object JSON payload"
            )

        accept_warning_status = payload.get("accept_warning_status")
        if not isinstance(accept_warning_status, bool):
            accept_warning_status = None
        raw_rationale = payload.get("rationale")
        rationale = (
            raw_rationale.strip()
            if isinstance(raw_rationale, str) and raw_rationale.strip()
            else None
        )
        if accept_warning_status is None and rationale is None:
            return None
        return VerificationBrainSuggestion(
            accept_warning_status=accept_warning_status,
            rationale=rationale,
        )
