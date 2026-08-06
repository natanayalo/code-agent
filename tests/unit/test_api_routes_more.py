"""Unit tests for additional API routes: health, metrics, sessions, system, knowledge_base."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from apps.api.config import SystemConfig
from apps.api.routes.health import health, ready
from apps.api.routes.knowledge_base import (
    get_knowledge_base_stats,
    get_memory_observation,
    list_memory_observations,
    list_memory_proposals,
)
from apps.api.routes.metrics import get_metrics
from apps.api.routes.sessions import get_session, list_sessions
from apps.api.routes.system import get_runtime_manifest, get_sandbox_status, list_tools
from orchestrator.execution import TaskExecutionService


def test_health():
    res = health()
    assert res.status == "ok"


def test_ready_unconfigured():
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(task_service=None)))
    resp = Response()

    snapshot = ready(req, resp)
    assert snapshot.status == "not_ready"
    assert resp.status_code == 503


def test_ready_configured():
    task_service = MagicMock(spec=TaskExecutionService)
    mock_snap = MagicMock()
    mock_snap.status = "ready"
    task_service.get_readiness.return_value = mock_snap

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(task_service=task_service)))
    resp = Response()

    snapshot = ready(req, resp)
    assert snapshot == mock_snap
    assert resp.status_code == 200


def test_get_metrics():
    task_service = MagicMock()
    task_service.get_operational_metrics.return_value = {"tasks": 10}
    assert get_metrics(task_service=task_service, window_hours=12) == {"tasks": 10}
    task_service.get_operational_metrics.assert_called_once_with(window_hours=12)


def test_list_sessions():
    task_service = MagicMock()
    task_service.list_sessions.return_value = []
    assert list_sessions(limit=10, offset=0, task_service=task_service) == []


def test_get_session():
    task_service = MagicMock()
    mock_session = MagicMock()
    task_service.get_session.return_value = mock_session
    assert get_session("s1", task_service=task_service) == mock_session

    task_service.get_session.return_value = None
    with pytest.raises(HTTPException) as exc:
        get_session("nonexistent", task_service=task_service)
    assert exc.value.status_code == 404


def test_system_routes():
    tools = list_tools()
    assert len(tools) > 0

    config = SystemConfig(default_image="img-1", workspace_root="/root")
    sandbox = get_sandbox_status(config=config)
    assert sandbox.default_image == "img-1"
    assert sandbox.workspace_root == "/root"

    manifest = get_runtime_manifest(config=config)
    assert manifest.sandbox.default_image == "img-1"


def test_knowledge_base_stats_and_observations():
    task_service = MagicMock()
    task_service.get_knowledge_base_stats.return_value = MagicMock()
    get_knowledge_base_stats(repo_url="http://repo", task_service=task_service)
    task_service.get_knowledge_base_stats.assert_called_once_with(repo_url="http://repo")

    task_service.list_memory_proposals.return_value = []
    assert list_memory_proposals(task_service=task_service) == []

    task_service.list_memory_observations.return_value = []
    assert list_memory_observations(task_service=task_service) == []

    mock_obs = MagicMock()
    task_service.get_memory_observation.return_value = mock_obs
    assert get_memory_observation("obs1", task_service=task_service) == mock_obs

    task_service.get_memory_observation.return_value = None
    with pytest.raises(HTTPException) as exc:
        get_memory_observation("nonexistent", task_service=task_service)
    assert exc.value.status_code == 404


def test_knowledge_base_proposals():
    task_service = MagicMock()

    from apps.api.routes.knowledge_base import (
        accept_memory_proposal,
        create_memory_proposal,
        list_memory_admission_decisions,
        reject_memory_proposal,
    )

    task_service.list_memory_admission_decisions.return_value = []
    assert list_memory_admission_decisions(task_service=task_service) == []

    payload = MagicMock()
    task_service.create_memory_proposal.return_value = MagicMock()
    create_memory_proposal(payload, task_service=task_service)

    # accept proposal
    task_service.accept_memory_proposal.return_value = ("ok", MagicMock(), None)
    accept_memory_proposal("p1", task_service=task_service)
    task_service.accept_memory_proposal.return_value = ("not_found", None, "Missing")
    with pytest.raises(HTTPException) as exc1:
        accept_memory_proposal("p1", task_service=task_service)
    assert exc1.value.status_code == 404

    task_service.accept_memory_proposal.return_value = ("conflict", None, "Conflict")
    with pytest.raises(HTTPException) as exc2:
        accept_memory_proposal("p1", task_service=task_service)
    assert exc2.value.status_code == 409

    task_service.accept_memory_proposal.return_value = ("ok", None, None)
    with pytest.raises(HTTPException) as exc3:
        accept_memory_proposal("p1", task_service=task_service)
    assert exc3.value.status_code == 500

    # reject proposal
    task_service.reject_memory_proposal.return_value = ("ok", MagicMock(), None)
    reject_memory_proposal("p1", task_service=task_service)
    task_service.reject_memory_proposal.return_value = ("not_found", None, "Missing")
    with pytest.raises(HTTPException) as exc4:
        reject_memory_proposal("p1", task_service=task_service)
    assert exc4.value.status_code == 404

    task_service.reject_memory_proposal.return_value = ("conflict", None, "Conflict")
    with pytest.raises(HTTPException) as exc5:
        reject_memory_proposal("p1", task_service=task_service)
    assert exc5.value.status_code == 409

    task_service.reject_memory_proposal.return_value = ("ok", None, None)
    with pytest.raises(HTTPException) as exc6:
        reject_memory_proposal("p1", task_service=task_service)
    assert exc6.value.status_code == 500


def test_knowledge_base_personal_and_project_memory():
    task_service = MagicMock()

    from apps.api.routes.knowledge_base import (
        delete_personal_memory,
        delete_project_memory,
        list_personal_memory,
        list_project_memory,
        search_personal_memory,
        search_project_memory,
        upsert_personal_memory,
        upsert_project_memory,
    )

    # personal memory
    task_service.list_personal_memory.return_value = []
    assert list_personal_memory(task_service=task_service) == []

    task_service.search_personal_memory.return_value = []
    assert search_personal_memory(q="test", task_service=task_service) == []

    task_service.upsert_personal_memory.return_value = MagicMock()
    upsert_personal_memory(MagicMock(), task_service=task_service)

    task_service.delete_personal_memory.return_value = True
    delete_personal_memory(memory_key="k1", task_service=task_service)
    task_service.delete_personal_memory.return_value = False
    with pytest.raises(HTTPException) as exc_p:
        delete_personal_memory(memory_key="k1", task_service=task_service)
    assert exc_p.value.status_code == 404

    # project memory
    task_service.list_project_memory.return_value = []
    assert list_project_memory(task_service=task_service) == []

    task_service.search_project_memory.return_value = []
    assert search_project_memory(repo_url="r1", q="test", task_service=task_service) == []

    task_service.upsert_project_memory.return_value = MagicMock()
    upsert_project_memory(MagicMock(), task_service=task_service)

    task_service.delete_project_memory.return_value = True
    delete_project_memory(repo_url="r1", memory_key="k1", task_service=task_service)
    task_service.delete_project_memory.return_value = False
    with pytest.raises(HTTPException) as exc_pr:
        delete_project_memory(repo_url="r1", memory_key="k1", task_service=task_service)
    assert exc_pr.value.status_code == 404
