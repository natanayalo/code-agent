"""Pydantic schemas for the Reflection and Improvement Pipeline."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from orchestrator.state import OrchestratorModel

FrictionSource = Literal["tooling", "orchestrator", "sandbox", "instructions", "other"]
ImpactLevel = Literal["slowed_down", "blocked", "required_workaround", "unknown"]
ValueScore = Literal["low", "medium", "high"]
EffortScore = Literal["small", "medium", "large"]
RiskScore = Literal["low", "medium", "high"]
LayerImpact = Literal["orchestrator", "worker", "sandbox", "api", "dashboard", "other"]
HitlNeed = Literal["required", "optional", "none"]


class FrictionReport(OrchestratorModel):
    """A structured report capturing friction encountered during a task execution."""

    task_id: str | None = None
    worker_run_id: str | None = None
    source: FrictionSource = "other"
    description: str = Field(min_length=1)
    impact: ImpactLevel = "unknown"
    context: dict[str, str] = Field(default_factory=dict)


class ImprovementSuggestion(OrchestratorModel):
    """A structured proposal for an improvement generated from friction or exploration."""

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    value: ValueScore = "medium"
    effort: EffortScore = "medium"
    risk: RiskScore = "medium"
    layer_impact: LayerImpact = "other"
    validation_path: str = Field(min_length=1)
    hitl_need: HitlNeed = "optional"
