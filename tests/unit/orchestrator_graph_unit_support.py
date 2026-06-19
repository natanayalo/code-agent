# ruff: noqa: F401
"""Shared fixtures and helpers for orchestrator graph unit tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.brain import (
    RouteBrainSuggestion,
    TaskSpecBrainSuggestion,
    UnifiedOrchestratorSuggestion,
)
from orchestrator.checkpoints import create_in_memory_checkpointer
from orchestrator.graph import (
    _await_worker_with_timeout,
    _build_worker_request,
    _coerce_approval_decision,
    _compute_route_decision,
    _default_worker_result_provider,
    _is_interaction_requirement_resolved,
    _resolve_orchestrator_timeout_seconds,
    _route_after_review_result,
    _task_requires_approval,
    await_approval,
    await_clarification,
    await_permission_escalation,
    build_choose_worker_node,
    build_generate_task_spec_and_route_node,
    choose_worker,
    dispatch_job,
    generate_task_spec,
    plan_task,
    summarize_result,
    verify_result,
)
from orchestrator.nodes.utils import (
    _classify_task_kind,
    _ensure_state,
    _has_meaningful_deliverable,
    _requires_deliverable_evidence,
)
from orchestrator.state import OrchestratorState
from orchestrator.task_spec import is_destructive_task
from workers import Worker, WorkerProfile, WorkerRequest, WorkerResult

_ALL_WORKERS: frozenset[str] = frozenset({"codex", "antigravity", "openrouter"})
_CODEX_ONLY: frozenset[str] = frozenset({"codex"})
_ANTIGRAVITY_ONLY: frozenset[str] = frozenset({"antigravity"})
_OPENROUTER_ONLY: frozenset[str] = frozenset({"openrouter"})

_PROFILED_CODEX_ANTIGRAVITY: dict[str, WorkerProfile] = {
    "codex-native-executor": WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
        capability_tags=["execution"],
        supported_delivery_modes=["workspace", "branch", "draft_pr"],
    ),
    "antigravity-native-executor": WorkerProfile(
        name="antigravity-native-executor",
        worker_type="antigravity",
        runtime_mode="native_agent",
        capability_tags=["execution"],
        supported_delivery_modes=["workspace", "branch", "draft_pr"],
    ),
}

_PROFILED_CODEX_OPENROUTER: dict[str, WorkerProfile] = {
    "codex-native-executor": WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
        capability_tags=["execution"],
        supported_delivery_modes=["workspace", "branch", "draft_pr"],
    ),
    "openrouter-tool-loop-legacy": WorkerProfile(
        name="openrouter-tool-loop-legacy",
        worker_type="openrouter",
        runtime_mode="tool_loop",
        capability_tags=["execution"],
        supported_delivery_modes=["workspace"],
    ),
}

__all__ = [name for name in globals() if not name.startswith("__")]
