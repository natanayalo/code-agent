"""Pydantic schemas for the Reflection and Improvement Pipeline."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FrictionSource = Literal["tooling", "orchestrator", "sandbox", "instructions", "other"]
ImpactLevel = Literal["slowed_down", "blocked", "required_workaround", "unknown"]
ValueScore = Literal["low", "medium", "high"]
EffortScore = Literal["small", "medium", "large"]
RiskScore = Literal["low", "medium", "high"]
LayerImpact = Literal["orchestrator", "worker", "sandbox", "api", "dashboard", "other"]
HitlNeed = Literal["required", "optional", "none"]


class FrictionReport(BaseModel):
    """A structured report capturing friction encountered during a task execution."""

    model_config = ConfigDict(extra="forbid")

    task_id: str | None = None
    worker_run_id: str | None = None
    source: FrictionSource | None = "other"
    description: str | None = Field(default=None, min_length=1)
    impact: ImpactLevel | None = "unknown"
    context: dict[str, Any] | None = Field(default=None)


class ImprovementSuggestion(BaseModel):
    """A structured proposal for an improvement generated from friction or exploration."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    value: ValueScore = "medium"
    effort: EffortScore = "medium"
    risk: RiskScore = "medium"
    layer_impact: LayerImpact = "other"
    validation_path: str = Field(min_length=1)
    hitl_need: HitlNeed = "optional"
