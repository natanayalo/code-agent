"""Optional orchestrator-brain contracts for TaskSpec enrichment."""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from orchestrator.state import (
    OrchestratorModel,
    TaskDeliveryMode,
    TaskPlan,
    TaskRequest,
    TaskRiskLevel,
    TaskSpec,
    TaskSpecType,
)


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


class RuleBasedOrchestratorBrain:
    """Small deterministic brain used as a safe bootstrap for optional enrichment."""

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
