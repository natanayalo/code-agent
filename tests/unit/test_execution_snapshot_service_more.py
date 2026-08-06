"""Unit tests for execution_snapshot_service.py methods."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from orchestrator.execution_snapshot_service import (
    _memory_count_snapshot,
    _optional_scope,
    delete_personal_memory,
    delete_project_memory,
    get_knowledge_base_stats,
    get_memory_observation,
    get_session,
    get_task,
    is_execution_busy,
    list_memory_admission_decisions,
    list_memory_observations,
    list_memory_proposals,
    list_personal_memory,
    list_project_memory,
    list_sessions,
    list_tasks,
    search_personal_memory,
    search_project_memory,
)


def test_optional_scope():
    assert _optional_scope(None) is None
    assert _optional_scope("   ") is None
    assert _optional_scope("  repo  ") == "repo"


def test_memory_count_snapshot():
    snap = _memory_count_snapshot((10, 2))
    assert snap.total == 10
    assert snap.requires_verification == 2


def test_snapshot_service_methods():
    svc = MagicMock()
    svc.session_factory = MagicMock()
    sess = MagicMock()
    svc.session_factory.return_value.__enter__.return_value = sess

    # is_execution_busy
    with patch("orchestrator.execution_snapshot_service.TaskRepository") as mock_task_repo_cls:
        mock_task_repo_cls.return_value.is_execution_busy.return_value = False
        assert is_execution_busy(svc) is False

    # get_task None
    svc._map_task_to_snapshot.return_value = None
    sess.scalar.return_value = None
    with patch("orchestrator.execution_snapshot_service.session_scope") as mock_scope:
        mock_scope.return_value.__enter__.return_value = sess
        sess.scalar.return_value = None
        assert get_task(svc, "t1") is None

    # list_tasks
    sess.scalars.return_value.all.return_value = []
    assert list_tasks(svc) == []

    # list_sessions
    with patch("orchestrator.execution_snapshot_service.SessionRepository") as mock_sess_repo_cls:
        mock_sess_repo_cls.return_value.list_all.return_value = []
        mock_sess_repo_cls.return_value.get.return_value = None
        assert list_sessions(svc) == []
        assert get_session(svc, "s1") is None

    # list_personal_memory
    assert list_personal_memory(svc) == []

    # get_knowledge_base_stats
    stats = get_knowledge_base_stats(svc, repo_url="  https://github.com/org/repo  ")
    assert stats.personal is not None

    # search_personal_memory
    assert search_personal_memory(svc, query="q") == []

    # delete_personal_memory
    with patch(
        "orchestrator.execution_snapshot_service.PersonalMemoryRepository"
    ) as mock_pers_repo_cls:
        mock_pers_repo_cls.return_value.delete.return_value = False
        assert delete_personal_memory(svc, memory_key="k") is False

    # list_project_memory
    assert list_project_memory(svc) == []

    # search_project_memory
    assert search_project_memory(svc, repo_url="r", query="q") == []

    # delete_project_memory
    with patch(
        "orchestrator.execution_snapshot_service.ProjectMemoryRepository"
    ) as mock_proj_repo_cls:
        mock_proj_repo_cls.return_value.delete.return_value = False
        assert delete_project_memory(svc, repo_url="r", memory_key="k") is False

    # get_memory_observation
    svc._map_memory_observation_to_snapshot.return_value = None
    with patch(
        "orchestrator.execution_snapshot_service.ObservationRepository"
    ) as mock_obs_repo_cls:
        mock_obs_repo_cls.return_value.get.return_value = None
        mock_obs_repo_cls.return_value.list.return_value = []
        assert get_memory_observation(svc, observation_id="o1") is None
        assert list_memory_observations(svc) == []

    # list_memory_admission_decisions
    assert list_memory_admission_decisions(svc) == []

    # list_memory_proposals
    assert list_memory_proposals(svc) == []
