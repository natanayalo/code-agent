"""Compact contracts used by the Temporal one-node-wave coordinator."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field

from orchestrator.node_execution import NodeActivityRequest, NodeActivityResultRef
from orchestrator.state import OrchestratorModel


class DecomposeTaskResult(OrchestratorModel):
    """The only decomposition decision carried through workflow history."""

    execution_shape: Literal["monolithic", "decomposed"]
    execution_task_queue: str | None = None


class NodeSelectionResult(OrchestratorModel):
    """A read-only, deterministic instruction for exactly one node wave."""

    action: Literal["execute", "merge_terminal", "skip", "await_permission", "complete", "invalid"]
    activity_request: NodeActivityRequest | None = None
    execution_task_queue: str | None = None
    node_id: str | None = None
    logical_activity_key: str | None = None
    result_digest: str | None = None
    reason: str | None = None
    failed_dependency_ids: list[str] = Field(default_factory=list)


class NodeWaveMergeRequest(OrchestratorModel):
    """Small merge command; full results remain in durable node storage."""

    selection: NodeSelectionResult
    result_ref: NodeActivityResultRef | None = None


class NodeWaveMergeResult(OrchestratorModel):
    """The sole controller output allowed to advance a node-wave workflow."""

    continuation: Literal["continue", "retry_node", "await_permission", "fail_task"]
    blocked_node_id: str | None = None
    blocked_logical_activity_key: str | None = None
    requested_permission: str | None = None


class NodeWaveItem(OrchestratorModel):
    """One ordered, independently durable execution in a bounded wave."""

    node_id: str
    activity_request: NodeActivityRequest
    execution_task_queue: str


class NodeWaveSelectionV2(OrchestratorModel):
    """Versioned fan-out selection; older histories keep ``NodeSelectionResult``."""

    schema_version: Literal[2] = 2
    action: Literal[
        "execute_wave", "merge_terminal_wave", "skip", "await_permission", "complete", "invalid"
    ]
    wave_id: str | None = None
    items: list[NodeWaveItem] = Field(default_factory=list, max_length=2)
    fanout_applied: bool = False
    reason: str | None = None


class NodeWaveMergeRequestV2(OrchestratorModel):
    """Ordered compact evidence references for a fan-out wave."""

    selection: NodeWaveSelectionV2
    result_refs: list[NodeActivityResultRef | None] = Field(default_factory=list, max_length=2)


def deterministic_wave_id(plan_id: str, items: list[NodeWaveItem]) -> str:
    """Return a completion-order-independent identity for a selected wave."""
    keys = [item.activity_request.logical_activity_key for item in items]
    digest = hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode()).hexdigest()
    return f"node-wave:v2:{plan_id}:{digest}"
