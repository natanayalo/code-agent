"""Import compatibility tests for decomposed modules."""

from __future__ import annotations

from orchestrator import execution as execution_module
from orchestrator.execution_policy import _validate_callback_url
from repositories import (
    HumanInteractionRepository,
    SessionRepository,
    TaskRepository,
    WorkerRunRepository,
)
from repositories.sqlalchemy_interaction import (
    HumanInteractionRepository as SqlHumanInteractionRepository,
)
from repositories.sqlalchemy_run import WorkerRunRepository as SqlWorkerRunRepository
from repositories.sqlalchemy_session import SessionRepository as SqlSessionRepository
from repositories.sqlalchemy_task import TaskRepository as SqlTaskRepository
from workers import cli_runtime as cli_runtime_module
from workers.cli_runtime_types import settings_from_budget


def test_orchestrator_execution_reexports_validation_helpers() -> None:
    assert execution_module._validate_callback_url is _validate_callback_url


def test_cli_runtime_reexports_budget_helpers() -> None:
    assert cli_runtime_module.settings_from_budget is settings_from_budget


def test_repositories_package_reexports_split_repository_classes() -> None:
    assert SessionRepository is SqlSessionRepository
    assert TaskRepository is SqlTaskRepository
    assert HumanInteractionRepository is SqlHumanInteractionRepository
    assert WorkerRunRepository is SqlWorkerRunRepository
