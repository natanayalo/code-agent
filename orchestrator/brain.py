"""Optional orchestrator-brain contracts for TaskSpec enrichment and routing."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any, Final, Protocol, cast, get_args

from pydantic import Field

from apps.observability import (
    add_current_span_event,
    set_current_span_attribute,
    set_span_status_from_outcome,
)
from db.enums import WorkerRuntimeMode, coerce_worker_type
from orchestrator.constants import RISK_ORDER
from orchestrator.improvement_suggestions import (
    ImprovementSuggestionScoringContext,
    ImprovementSuggestionScoringMetadata,
    ImprovementSuggestionScoringResult,
)
from orchestrator.reflection import (
    EffortScore,
    FrictionReport,
    HitlNeed,
    ImprovementSuggestion,
    LayerImpact,
    RiskScore,
    ValueScore,
)
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
from workers.base import Worker, WorkerProfile, WorkerRequest, WorkerResult
from workers.constants import DEFAULT_DISCOVERY_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


def _planner_failure_reason_code(result: WorkerResult | None, exc: Exception | None = None) -> str:
    if exc is not None:
        if isinstance(exc, TimeoutError | asyncio.TimeoutError):
            return "timeout"
        return "exception"
    if result is None:
        return "unknown_error"
    summary = (result.summary or "").lower()
    if result.status == "error" and ("timed out" in summary or "timeout" in summary):
        return "timeout"
    if result.failure_kind:
        return str(result.failure_kind)
    return result.status


DEFAULT_ROUTE_BRAIN_TIMEOUT_SECONDS = DEFAULT_DISCOVERY_TIMEOUT_SECONDS
DEFAULT_TASK_SPEC_BRAIN_TIMEOUT_SECONDS = DEFAULT_DISCOVERY_TIMEOUT_SECONDS
DEFAULT_ROUTE_PLANNER_PROFILE = "antigravity-native-discovery"
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
    "suggested_worker": "codex" | "antigravity" | "openrouter" | null,
    "suggested_profile": "<profile-name>" | null,
    "suggested_retry_strategy": "retry_same_worker" | "escalate_to_alternate" | null,
    "rationale": "<short reason>"
  }
- Set at least one of suggested_worker, suggested_profile, or suggested_retry_strategy.
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
- The authoritative JSON schema will be provided in the Output section.
- Follow that schema exactly and do not add extra keys.
""".strip()

_IMPROVEMENT_SCORING_SYSTEM_PROMPT = """
You are an improvement proposal scoring assistant.

Task:
- Review one friction report and the deterministic improvement suggestion derived from it.
- Return revised scoring fields only when the evidence supports a better score.
- Keep the output conservative, operational, and grounded in the provided evidence.

Output contract:
- Return exactly one JSON object, and no extra prose.
- The authoritative JSON schema will be provided in the Output section.
- Follow that schema exactly and do not add extra keys.
""".strip()

_RULES_RATIONALE = "rules_v1"
_NATIVE_WRAPPER_METADATA_KEYS = frozenset({"session_id", "stats", "models", "tools", "files"})
_IMPROVEMENT_SCORING_LITERAL_FIELDS = frozenset(
    {"value", "effort", "risk", "layer_impact", "hitl_need"}
)


def _native_wrapper_payload_key(payload: Mapping[str, Any]) -> str | None:
    """Return the wrapper payload key only for native/LLM metadata envelopes."""
    for key in ("response", "content"):
        if key in payload:
            return key
    if payload.keys() & _NATIVE_WRAPPER_METADATA_KEYS:
        if "summary" in payload:
            return "summary"
    return None


def extract_json_block(text: str) -> str:
    """Extract a JSON payload from markdown fences or the first balanced { ... } block."""
    stripped = text.strip()
    if not stripped:
        return stripped

    # 1. Try to find the last markdown fenced block first
    # (models often emit examples before final output)
    fenced_matches = re.findall(
        r"```(?:json)?\s*(.*?)```",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced_matches:
        candidate = fenced_matches[-1].strip()
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, str):
                try:
                    json.loads(parsed)
                    return parsed
                except (json.JSONDecodeError, TypeError):
                    pass
            return candidate
        except (json.JSONDecodeError, TypeError):
            # Let it fall through to step 2 which parses the outer wrapper
            pass

    # 2. Try to find the first '{' and use a decoder to get the first valid object.
    # This prevents "Extra data" errors when models append prose or extra JSON
    # after the main payload.
    # We iterate forwards to find the first valid JSON object, which is
    # usually the intended payload.
    decoder = json.JSONDecoder()
    for i in range(len(stripped)):
        if stripped[i] == "{":
            try:
                obj, end_idx = decoder.raw_decode(stripped[i:])
                # Verify that the content after the JSON block is only whitespace/punctuation
                # (or we just trust the last one found from the end)
                raw_json = stripped[i : i + end_idx].strip()
                # Unwrapping: if the JSON includes a known wrapper key, look inside it.
                # Native runners may include metadata alongside response content
                # (for example: {"session_id": ..., "response": "...", "stats": ...}).
                data = obj
                if isinstance(data, dict) and (wrapper_key := _native_wrapper_payload_key(data)):
                    inner = data.get(wrapper_key)
                    if isinstance(inner, str | dict):
                        return extract_json_block(
                            inner if isinstance(inner, str) else json.dumps(inner)
                        )
                return raw_json
            except (json.JSONDecodeError, ValueError):
                continue

    # 3. Fall back to greedy brace matching for malformed but potentially parseable JSON
    start_idx = stripped.find("{")
    end_idx = stripped.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        return stripped[start_idx : end_idx + 1]

    # 4. Final fallback to the original stripped text
    return stripped


def _strict_json_schema(model_cls: type[OrchestratorModel]) -> dict[str, Any]:
    """
    Generate a strict JSON schema where all properties are marked as required.
    This is necessary for OpenAI Structured Outputs (e.g. via Codex) which enforce
    that every field in the object properties must be listed in the `required` array,
    even if the type allows `null`.
    """
    schema = model_cls.model_json_schema()
    if "properties" in schema:
        schema["required"] = list(schema["properties"].keys())
    return schema


def _coerce_worker_type(value: object) -> WorkerType | None:
    """Normalize string worker values to the supported vocabulary."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    try:
        return cast(WorkerType, coerce_worker_type(normalized))
    except ValueError:
        return None


def _coerce_literal_string(value: object, allowed: tuple[str, ...]) -> str | None:
    """Return a normalized literal string when it belongs to the allowed vocabulary."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in allowed else None


def _coerce_string_list(value: object) -> list[str]:
    """Best-effort coercion of list-like payload fields to non-empty strings."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                out.append(stripped)
    return out


def _coerce_unified_suggestion_tolerant(
    payload: Mapping[str, Any],
) -> UnifiedOrchestratorSuggestion:
    """
    Parse unified suggestion payload with tolerant coercion.

    Invalid enum-like fields are clamped to None while preserving valid route hints
    such as suggested_worker/suggested_profile.
    """
    risk_allowed = cast(tuple[str, ...], get_args(TaskRiskLevel))
    task_type_allowed = cast(tuple[str, ...], get_args(TaskSpecType))
    delivery_mode_allowed = cast(tuple[str, ...], get_args(TaskDeliveryMode))
    suggested_profile_raw = payload.get("suggested_profile")
    suggested_profile = (
        suggested_profile_raw.strip()
        if isinstance(suggested_profile_raw, str) and suggested_profile_raw.strip()
        else None
    )
    rationale_raw = payload.get("rationale")
    rationale = (
        rationale_raw.strip() if isinstance(rationale_raw, str) and rationale_raw.strip() else None
    )
    return UnifiedOrchestratorSuggestion(
        assumptions=_coerce_string_list(payload.get("assumptions")),
        acceptance_criteria=_coerce_string_list(payload.get("acceptance_criteria")),
        non_goals=_coerce_string_list(payload.get("non_goals")),
        clarification_questions=_coerce_string_list(payload.get("clarification_questions")),
        verification_commands=_coerce_string_list(payload.get("verification_commands")),
        suggested_risk_level=cast(
            TaskRiskLevel | None,
            _coerce_literal_string(payload.get("suggested_risk_level"), risk_allowed),
        ),
        suggested_task_type=cast(
            TaskSpecType | None,
            _coerce_literal_string(payload.get("suggested_task_type"), task_type_allowed),
        ),
        suggested_delivery_mode=cast(
            TaskDeliveryMode | None,
            _coerce_literal_string(payload.get("suggested_delivery_mode"), delivery_mode_allowed),
        ),
        suggested_worker=_coerce_worker_type(payload.get("suggested_worker")),
        suggested_profile=suggested_profile,
        suggested_retry_strategy=(
            payload.get("suggested_retry_strategy")
            if isinstance(payload.get("suggested_retry_strategy"), str | type(None))
            else None
        ),
        rationale=rationale,
    )


def _unwrap_payload_wrapper(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """
    Unwrap native-runner metadata envelopes when they contain a JSON response body.

    Example wrapper:
    {"session_id":"...","response":"```json ... ```","stats":{...}}
    """
    wrapper_key = _native_wrapper_payload_key(payload)
    if wrapper_key is None:
        return payload

    inner = payload.get(wrapper_key)
    if isinstance(inner, dict):
        return inner
    if isinstance(inner, str):
        normalized_json = extract_json_block(inner)
        try:
            candidate = json.loads(normalized_json)
        except json.JSONDecodeError:
            return payload
        if isinstance(candidate, dict):
            return candidate
    return payload


def _looks_like_unified_payload(payload: Mapping[str, Any]) -> bool:
    """Heuristic to distinguish suggestion payloads from telemetry/stats payloads."""
    expected_keys = {
        "assumptions",
        "acceptance_criteria",
        "non_goals",
        "clarification_questions",
        "verification_commands",
        "suggested_risk_level",
        "suggested_task_type",
        "suggested_delivery_mode",
        "suggested_worker",
        "suggested_profile",
        "suggested_retry_strategy",
        "rationale",
    }
    return bool(expected_keys.intersection(payload.keys()))


def _extract_unified_payload_from_summary(summary: str) -> Mapping[str, Any] | None:
    """
    Extract the best unified suggestion payload from free-form summary text.

    Preference order:
    1. Any fenced JSON blocks that look like unified payloads.
    2. The generic extract_json_block fallback when it looks like unified payload.
    """
    text = summary.strip()
    if not text:
        return None

    fenced_matches = re.findall(
        r"```(?:json)?\s*(.*?)```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for candidate in reversed(fenced_matches):
        candidate_text = candidate.strip()
        try:
            payload = json.loads(candidate_text)
        except json.JSONDecodeError:
            # It may be escaped inside a wrapper, let it fall through
            continue

        if isinstance(payload, str):
            try:
                payload, _ = json.JSONDecoder().raw_decode(payload.strip())
            except Exception as e:
                logger.debug("Failed to decode payload: %s", e)

        if isinstance(payload, dict):
            unwrapped = _unwrap_payload_wrapper(payload)
            if _looks_like_unified_payload(unwrapped):
                return unwrapped

    normalized_json = extract_json_block(text)
    try:
        payload = json.loads(normalized_json)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        unwrapped = _unwrap_payload_wrapper(payload)
        if _looks_like_unified_payload(unwrapped):
            return unwrapped
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
    suggested_delivery_branch: str | None = None
    suggested_pr_title: str | None = None
    suggested_pr_body: str | None = None
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


class UnifiedOrchestratorSuggestion(OrchestratorModel):
    """Structured unified suggestion for TaskSpec enrichment and route recommendation."""

    assumptions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    suggested_risk_level: TaskRiskLevel | None = None
    suggested_task_type: TaskSpecType | None = None
    suggested_delivery_mode: TaskDeliveryMode | None = None
    suggested_delivery_branch: str | None = None
    suggested_pr_title: str | None = None
    suggested_pr_body: str | None = None
    suggested_worker: WorkerType | None = None
    suggested_profile: str | None = None
    suggested_retry_strategy: str | None = None
    rationale: str | None = None


class ImprovementScoringBrainSuggestion(OrchestratorModel):
    """Optional model-backed score overrides for an improvement suggestion."""

    value: ValueScore | None = None
    effort: EffortScore | None = None
    risk: RiskScore | None = None
    layer_impact: LayerImpact | None = None
    validation_path: str | None = Field(default=None, min_length=1)
    hitl_need: HitlNeed | None = None
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

    async def suggest_task_spec_and_route(
        self,
        *,
        state: OrchestratorState,
        task_spec: TaskSpec,
        available_workers: frozenset[str],
        available_profiles: Mapping[str, WorkerProfile] | None = None,
    ) -> UnifiedOrchestratorSuggestion | None:
        """Return unified task-spec enrichment and route suggestion, or None."""

    async def score_improvement_suggestion(
        self,
        *,
        report: FrictionReport,
        deterministic_suggestion: ImprovementSuggestion,
        context: ImprovementSuggestionScoringContext,
    ) -> ImprovementSuggestionScoringResult | None:
        """Return model-scored improvement fields, or None to keep deterministic scoring."""


class RuleBasedOrchestratorBrain:
    """Small deterministic brain used as a safe bootstrap for optional enrichment."""

    def __init__(
        self,
        *,
        planner_worker: Worker | None = None,
        fallback_planners: list[Worker] | None = None,
        planner_profile: str = DEFAULT_ROUTE_PLANNER_PROFILE,
        planner_timeout_seconds: int = DEFAULT_ROUTE_BRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self.primary_planner = planner_worker
        self.fallback_planners = fallback_planners or []
        self.planner_profile = planner_profile.strip() or DEFAULT_ROUTE_PLANNER_PROFILE
        self.planner_timeout_seconds = (
            planner_timeout_seconds
            if planner_timeout_seconds > 0
            else DEFAULT_ROUTE_BRAIN_TIMEOUT_SECONDS
        )

    def _get_all_planners(self) -> list[Worker]:
        """Return the prioritized list of available planners."""
        planners: list[Worker] = []
        if self.primary_planner is not None:
            planners.append(self.primary_planner)
        planners.extend(self.fallback_planners)
        return planners

    async def score_improvement_suggestion(
        self,
        *,
        report: FrictionReport,
        deterministic_suggestion: ImprovementSuggestion,
        context: ImprovementSuggestionScoringContext,
    ) -> ImprovementSuggestionScoringResult | None:
        """Use planner workers to revise improvement proposal scoring fields."""
        planners = self._get_all_planners()
        if not planners:
            return None

        request = self._build_improvement_scoring_request(
            report=report,
            deterministic_suggestion=deterministic_suggestion,
            context=context,
        )
        result, provider = await self._run_improvement_scoring_planners(planners, request)
        if result is None or provider is None:
            return None

        payload = self._improvement_scoring_payload(result)
        if payload is None:
            return None

        try:
            scoring = ImprovementScoringBrainSuggestion.model_validate(
                self._normalize_improvement_scoring_payload(payload)
            )
        except Exception as exc:
            logger.warning("Failed to validate brain improvement scoring suggestion: %s", exc)
            return None
        return self._merge_improvement_scoring(
            deterministic_suggestion=deterministic_suggestion,
            scoring=scoring,
            provider=provider,
        )

    def _build_improvement_scoring_request(
        self,
        *,
        report: FrictionReport,
        deterministic_suggestion: ImprovementSuggestion,
        context: ImprovementSuggestionScoringContext,
    ) -> WorkerRequest:
        prompt_payload = {
            "friction_report": report.model_dump(mode="json"),
            "deterministic_suggestion": deterministic_suggestion.model_dump(mode="json"),
            "task": {
                "task_id": context.task_id,
                "task_text": context.task_text,
                "attempt_count": context.attempt_count,
                "failure_kind": context.failure_kind,
                "retry_context": context.retry_context,
            },
        }
        context_json = json.dumps(_to_serializable(prompt_payload), sort_keys=True, default=str)
        prompt = (
            "Score this improvement suggestion from the provided friction evidence.\n\n"
            "Context JSON:\n"
            f"{context_json}\n"
        )
        constraints = dict(context.task_constraints or {})
        constraints["read_only"] = True
        constraints.pop("granted_permission", None)
        budget = dict(context.task_budget or {})
        budget["worker_timeout_seconds"] = self.planner_timeout_seconds
        return WorkerRequest(
            session_id=context.session_id,
            task_id=context.task_id,
            repo_url=context.repo_url,
            branch=context.branch,
            task_text=prompt,
            memory_context={},
            task_plan=None,
            task_spec=None,
            constraints=constraints,
            budget=budget,
            secrets={},
            tools=[],
            worker_profile=self.planner_profile,
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            response_format="json",
            response_schema=_strict_json_schema(ImprovementScoringBrainSuggestion),
        )

    async def _run_improvement_scoring_planners(
        self,
        planners: list[Worker],
        request: WorkerRequest,
    ) -> tuple[WorkerResult | None, str | None]:
        for i, worker in enumerate(planners):
            result: WorkerResult | None = None
            worker_name = worker.__class__.__name__
            try:
                async with asyncio.timeout(
                    self.planner_timeout_seconds + DEFAULT_BRAIN_TIMEOUT_BUFFER_SECONDS
                ):
                    result = await worker.run(
                        request,
                        system_prompt=_IMPROVEMENT_SCORING_SYSTEM_PROMPT,
                    )
                if result is not None and result.status == "success":
                    return result, worker_name
                if result is None:
                    logger.warning(
                        "planner improvement scoring returned no result (%s)",
                        worker_name,
                    )
                    continue
                logger.warning(
                    "planner improvement scoring returned non-success status '%s' (%s)",
                    result.status,
                    worker_name,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                logger.warning("planner improvement scoring timed out for %s", worker_name)
            except Exception as exc:
                logger.warning(
                    "planner improvement scoring failed for %s: %s: %s",
                    worker_name,
                    type(exc).__name__,
                    str(exc),
                )

        return None, None

    @staticmethod
    def _improvement_scoring_payload(result: WorkerResult) -> Mapping[str, Any] | None:
        payload: Mapping[str, Any] | None = result.json_payload
        if isinstance(payload, Mapping):
            return _unwrap_payload_wrapper(payload)

        raw_summary = (result.summary or "").strip()
        if not raw_summary:
            return None
        normalized_json = extract_json_block(raw_summary)
        try:
            decoded = json.loads(normalized_json)
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, Mapping):
            return None
        return _unwrap_payload_wrapper(decoded)

    @staticmethod
    def _normalize_improvement_scoring_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in payload.items():
            if key in _IMPROVEMENT_SCORING_LITERAL_FIELDS and isinstance(value, str):
                normalized[key] = value.strip().lower()
            else:
                normalized[key] = value
        return normalized

    @staticmethod
    def _merge_improvement_scoring(
        *,
        deterministic_suggestion: ImprovementSuggestion,
        scoring: ImprovementScoringBrainSuggestion,
        provider: str,
    ) -> ImprovementSuggestionScoringResult | None:
        updates: dict[str, Any] = {}
        for field_name in (
            "value",
            "effort",
            "risk",
            "layer_impact",
            "validation_path",
            "hitl_need",
        ):
            value = getattr(scoring, field_name)
            if value is not None:
                updates[field_name] = value
        rationale = (scoring.rationale or "").strip() or None
        if not updates and not rationale:
            return None

        payload = deterministic_suggestion.model_dump(mode="json")
        payload.update(updates)
        suggestion = ImprovementSuggestion.model_validate(payload)
        return ImprovementSuggestionScoringResult(
            suggestion=suggestion,
            metadata=ImprovementSuggestionScoringMetadata(
                enabled=True,
                mode="llm",
                provider=provider,
                rationale=rationale,
                fallback=False,
            ),
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

        # 2. Attempt model-backed enrichment if planners are available
        planners = self._get_all_planners()
        if planners:
            model_suggestion = await self._suggest_task_spec_model(
                task=task,
                task_kind=task_kind,
                task_plan=task_plan,
                task_spec=task_spec,
                planners=planners,
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
        planners: list[Worker],
    ) -> TaskSpecBrainSuggestion | None:
        """Use the planner workers to produce model-backed TaskSpec enrichment with fallback."""

        prompt_payload = {
            "task_text": task.task_text,
            "task_kind": task_kind,
            "deterministic_spec": task_spec.model_dump(mode="json"),
            "task_plan": task_plan.model_dump(mode="json") if task_plan else None,
            "constraints": dict(task.constraints),
        }
        context_json = json.dumps(_to_serializable(prompt_payload), sort_keys=True, default=str)
        prompt = f"Suggest TaskSpec enrichments for this task.\n\nContext JSON:\n{context_json}\n"

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
            response_format="json",
            response_schema=_strict_json_schema(TaskSpecBrainSuggestion),
        )
        for i, worker in enumerate(planners):
            result = None
            try:
                async with asyncio.timeout(
                    DEFAULT_TASK_SPEC_BRAIN_TIMEOUT_SECONDS + DEFAULT_BRAIN_TIMEOUT_BUFFER_SECONDS
                ):
                    result = await worker.run(request, system_prompt=_TASK_SPEC_SYSTEM_PROMPT)

                if result.status == "success":
                    break

                logger.warning(
                    f"planner task spec enrichment returned non-success status '{result.status}' "
                    f"({worker.__class__.__name__})"
                )
            except TimeoutError:
                logger.warning(
                    f"planner task spec enrichment timed out for {worker.__class__.__name__}"
                )
            except Exception as exc:
                logger.warning(
                    f"planner task spec enrichment failed for {worker.__class__.__name__}: "
                    f"{type(exc).__name__}: {exc}"
                )

            # If we're at the last worker and it failed, we'll return None
            if i == len(planners) - 1:
                if result is not None:
                    set_span_status_from_outcome(result.status, result.summary)
                return None

        # If we broke out of the loop, result should be set and successful
        if result is None or result.status != "success":
            return None

        data = result.json_payload
        if not data:
            raw_summary = (result.summary or "").strip()
            if not raw_summary:
                return None
            normalized_json = extract_json_block(raw_summary)
            try:
                data = json.loads(normalized_json)
            except Exception as exc:
                logger.warning(f"Failed to parse brain task spec suggestion: {exc}")
                return None

        try:
            return TaskSpecBrainSuggestion.model_validate(data)
        except Exception as exc:
            logger.warning(f"Failed to validate brain task spec suggestion: {exc}")
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
            suggested_delivery_branch=model.suggested_delivery_branch
            or base.suggested_delivery_branch,
            suggested_pr_title=model.suggested_pr_title or base.suggested_pr_title,
            suggested_pr_body=model.suggested_pr_body or base.suggested_pr_body,
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
        """Use the planner workers to produce a structured route recommendation with fallback."""
        planners = self._get_all_planners()
        if not planners:
            raise RuntimeError("planner route recommendation unavailable: no planners wired")

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

        previous_attempts_history = []
        if state.attempt_count > 1 and state.timeline_events:
            attempts_map: dict[int, dict[str, Any]] = {}
            for evt in state.timeline_events:
                a_num = evt.attempt_number
                if a_num is None or a_num >= state.attempt_count - 1:
                    continue
                if a_num not in attempts_map:
                    attempts_map[a_num] = {
                        "attempt": a_num,
                        "worker_dispatched": None,
                        "outcome": "unknown",
                        "message": None,
                    }

                evt_type = (
                    str(evt.event_type.value)
                    if hasattr(evt.event_type, "value")
                    else evt.event_type
                )
                if evt_type == "worker_dispatched":
                    w_type = None
                    if evt.payload and "worker_type" in evt.payload:
                        w_type = evt.payload["worker_type"]
                    attempts_map[a_num]["worker_dispatched"] = w_type or "unknown"
                elif evt_type in ("worker_failed", "worker_error", "task_failed"):
                    attempts_map[a_num]["outcome"] = "failed"
                    attempts_map[a_num]["message"] = evt.message
                elif evt_type in ("worker_completed", "task_completed"):
                    attempts_map[a_num]["outcome"] = "completed"
                    attempts_map[a_num]["message"] = evt.message
                elif evt_type == "infra_failure":
                    attempts_map[a_num]["outcome"] = "infra_failure"
                    attempts_map[a_num]["message"] = evt.message

            for a_num in sorted(attempts_map.keys()):
                if attempts_map[a_num]["worker_dispatched"] is None:
                    attempts_map[a_num]["outcome"] = "no_worker_executed_paused_or_interrupted"
                previous_attempts_history.append(attempts_map[a_num])

        task_text = state.normalized_task_text or state.task.task_text
        prompt_payload = {
            "task_text": task_text,
            "task_kind": state.task_kind,
            "attempt_count": state.attempt_count,
            "previous_attempts_history": previous_attempts_history,
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
            "Return the best route recommendation for this orchestration context.\n"
            "Note: 'attempt_count' tracks queue leases and may increment due to human "
            "interaction pauses. Always rely strictly on 'previous_attempts_history' to "
            "determine if a worker actually executed and failed on prior attempts.\n\n"
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
            tools=[],
            worker_profile=self.planner_profile,
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            response_format="json",
            response_schema=_strict_json_schema(RouteBrainSuggestion),
        )
        for i, worker in enumerate(planners):
            result = None
            try:
                async with asyncio.timeout(
                    self.planner_timeout_seconds + DEFAULT_BRAIN_TIMEOUT_BUFFER_SECONDS
                ):
                    result = await worker.run(request, system_prompt=_ROUTE_SYSTEM_PROMPT)

                if result.status == "success":
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "planner route recommendation failed for %s: %s: %s",
                    worker.__class__.__name__,
                    type(exc).__name__,
                    str(exc),
                )

            if i == len(planners) - 1:
                if result is not None:
                    set_span_status_from_outcome(result.status, result.summary)
                return None

        if result is None or result.status != "success":
            return None

        payload = result.json_payload
        if not payload:
            raw_summary = (result.summary or "").strip()
            if not raw_summary:
                return None
            normalized_json = extract_json_block(raw_summary)
            try:
                payload = json.loads(normalized_json)
            except json.JSONDecodeError:
                return None

        if not isinstance(payload, dict):
            return None

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
            return None
        return suggestion

    async def suggest_task_spec_and_route(
        self,
        *,
        state: OrchestratorState,
        task_spec: TaskSpec,
        available_workers: frozenset[str],
        available_profiles: Mapping[str, WorkerProfile] | None = None,
    ) -> UnifiedOrchestratorSuggestion | None:
        """Produce one unified suggestion envelope for TaskSpec and routing."""
        task_suggestion = self._suggest_task_spec_rules(
            task=state.task,
            task_kind=state.task_kind,
            task_plan=state.task_plan,
            task_spec=task_spec,
        ) or TaskSpecBrainSuggestion(rationale="brain_enrichment_v1")

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

        prompt_payload = {
            "task_text": state.normalized_task_text or state.task.task_text,
            "task_kind": state.task_kind,
            "deterministic_spec": task_spec.model_dump(mode="json"),
            "task_plan": state.task_plan.model_dump(mode="json") if state.task_plan else None,
            "constraints": dict(state.task.constraints),
            "budget": dict(state.task.budget),
            "available_workers": sorted(available_workers),
            "available_profiles": profiles_payload,
            "attempt_count": state.attempt_count,
            "dispatch_worker": state.dispatch.worker_type,
        }
        context_json = json.dumps(_to_serializable(prompt_payload), sort_keys=True, default=str)
        prompt = (
            "Suggest TaskSpec enrichments and best route recommendation.\n\n"
            "Context JSON:\n"
            f"{context_json}\n"
        )

        planners = self._get_all_planners()
        if planners:
            result: WorkerResult | None = None
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
                task_spec=task_spec.model_dump(mode="json"),
                constraints=constraints,
                budget=budget,
                secrets={},
                tools=[],
                worker_profile=self.planner_profile,
                runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                response_format="json",
                response_schema=_strict_json_schema(UnifiedOrchestratorSuggestion),
            )

            planner_failures: list[dict[str, str]] = []
            successful_planner: str | None = None
            for i, worker in enumerate(planners):
                result = None
                worker_name = worker.__class__.__name__
                try:
                    async with asyncio.timeout(
                        self.planner_timeout_seconds + DEFAULT_BRAIN_TIMEOUT_BUFFER_SECONDS
                    ):
                        result = await worker.run(
                            request,
                            system_prompt=_TASK_SPEC_SYSTEM_PROMPT + "\n\n" + _ROUTE_SYSTEM_PROMPT,
                        )
                    if result.status == "success":
                        successful_planner = worker_name
                        break
                    failure = {
                        "planner": worker_name,
                        "status": result.status,
                        "reason_code": _planner_failure_reason_code(result),
                    }
                    planner_failures.append(failure)
                    add_current_span_event("code_agent.brain.planner_failed", failure)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failure = {
                        "planner": worker_name,
                        "status": "error",
                        "reason_code": _planner_failure_reason_code(None, exc),
                    }
                    planner_failures.append(failure)
                    add_current_span_event("code_agent.brain.planner_failed", failure)
                    logger.warning(
                        "planner unified suggestion failed for %s: %s: %s",
                        worker_name,
                        type(exc).__name__,
                        str(exc),
                    )
                if i == len(planners) - 1:
                    break

            if planner_failures:
                set_current_span_attribute("code_agent.brain.planner_fallback.used", True)
                set_current_span_attribute(
                    "code_agent.brain.planner_fallback.from",
                    planner_failures[0]["planner"],
                )
                set_current_span_attribute(
                    "code_agent.brain.planner_fallback.reason_code",
                    planner_failures[0]["reason_code"],
                )
                if successful_planner:
                    set_current_span_attribute(
                        "code_agent.brain.planner_fallback.to", successful_planner
                    )
                elif result is not None:
                    set_span_status_from_outcome(result.status, result.summary)

            if planners and result is not None and result.status == "success":
                payload_source = "none"
                payload: Mapping[str, Any] | None = result.json_payload
                if isinstance(payload, Mapping):
                    payload_source = "json_payload"
                    unwrapped = _unwrap_payload_wrapper(payload)
                    payload = unwrapped if _looks_like_unified_payload(unwrapped) else None
                if not payload:
                    raw_summary = (result.summary or "").strip()
                    if raw_summary:
                        payload_source = "summary"
                        payload = _extract_unified_payload_from_summary(raw_summary)
                if isinstance(payload, Mapping):
                    payload = _unwrap_payload_wrapper(payload)
                    set_current_span_attribute("code_agent.brain.payload_source", payload_source)
                    set_current_span_attribute(
                        "code_agent.brain.raw_payload_keys",
                        ",".join(sorted(str(k) for k in payload.keys())),
                    )
                    try:
                        unified = UnifiedOrchestratorSuggestion.model_validate(payload)
                    except Exception as exc:
                        logger.warning("Failed to validate unified brain suggestion: %s", exc)
                        set_current_span_attribute(
                            "code_agent.brain.validation_fallback", "tolerant_coercion"
                        )
                        unified = _coerce_unified_suggestion_tolerant(payload)
                    set_current_span_attribute(
                        "code_agent.brain.parsed_suggested_worker",
                        unified.suggested_worker or "null",
                    )
                    set_current_span_attribute(
                        "code_agent.brain.parsed_suggested_profile",
                        unified.suggested_profile or "null",
                    )
                    task_suggestion = self._merge_task_spec_suggestions(
                        task_suggestion,
                        TaskSpecBrainSuggestion(
                            assumptions=unified.assumptions,
                            acceptance_criteria=unified.acceptance_criteria,
                            non_goals=unified.non_goals,
                            clarification_questions=unified.clarification_questions,
                            verification_commands=unified.verification_commands,
                            suggested_risk_level=unified.suggested_risk_level,
                            suggested_task_type=unified.suggested_task_type,
                            suggested_delivery_mode=unified.suggested_delivery_mode,
                            suggested_delivery_branch=unified.suggested_delivery_branch,
                            suggested_pr_title=unified.suggested_pr_title,
                            suggested_pr_body=unified.suggested_pr_body,
                            rationale=unified.rationale,
                        ),
                    )
                    return UnifiedOrchestratorSuggestion(
                        assumptions=task_suggestion.assumptions,
                        acceptance_criteria=task_suggestion.acceptance_criteria,
                        non_goals=task_suggestion.non_goals,
                        clarification_questions=task_suggestion.clarification_questions,
                        verification_commands=task_suggestion.verification_commands,
                        suggested_risk_level=task_suggestion.suggested_risk_level,
                        suggested_task_type=task_suggestion.suggested_task_type,
                        suggested_delivery_mode=task_suggestion.suggested_delivery_mode,
                        suggested_delivery_branch=task_suggestion.suggested_delivery_branch,
                        suggested_pr_title=task_suggestion.suggested_pr_title,
                        suggested_pr_body=task_suggestion.suggested_pr_body,
                        suggested_worker=unified.suggested_worker,
                        suggested_profile=unified.suggested_profile,
                        suggested_retry_strategy=unified.suggested_retry_strategy,
                        rationale=task_suggestion.rationale,
                    )
                set_current_span_attribute("code_agent.brain.payload_source", payload_source)

        if any(
            getattr(task_suggestion, field)
            for field in type(task_suggestion).model_fields
            if field != "rationale"
        ):
            return UnifiedOrchestratorSuggestion(
                assumptions=task_suggestion.assumptions,
                acceptance_criteria=task_suggestion.acceptance_criteria,
                non_goals=task_suggestion.non_goals,
                clarification_questions=task_suggestion.clarification_questions,
                verification_commands=task_suggestion.verification_commands,
                suggested_risk_level=task_suggestion.suggested_risk_level,
                suggested_task_type=task_suggestion.suggested_task_type,
                suggested_delivery_mode=task_suggestion.suggested_delivery_mode,
                suggested_delivery_branch=task_suggestion.suggested_delivery_branch,
                suggested_pr_title=task_suggestion.suggested_pr_title,
                suggested_pr_body=task_suggestion.suggested_pr_body,
                rationale=task_suggestion.rationale,
            )
        return None
