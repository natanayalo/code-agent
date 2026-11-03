"""Reconciliation credential selection stays scoped to delivery credentials."""

from unittest.mock import MagicMock, patch

from orchestrator.github_delivery_reconciliation import _task_github_token
from sandbox.secrets import EphemeralSecretRecord, SecretExposurePolicy, SecretScope


def test_broker_secret_takes_precedence_and_failures_do_not_leak(monkeypatch, caplog):
    monkeypatch.setenv("GH_TOKEN", "environment-token")
    task = MagicMock()
    task.id = "matched-task"
    task.secrets = {}
    task.secret_refs = [
        {"name": "other"},
        {"name": None},
        {"name": "ephem_failed"},
        {"name": "ephem_valid"},
    ]
    record = EphemeralSecretRecord(
        handle_id="ephem_valid",
        task_id=task.id,
        value="task-token",
        required_scope=SecretScope.GIT_PUSH,
        exposure_policy=SecretExposurePolicy.BROKER_ONLY,
    )
    store = MagicMock()
    store.get_record.side_effect = [RuntimeError("secret-value-should-not-appear"), record]
    with patch(
        "orchestrator.github_delivery_reconciliation.PostgresEphemeralSecretStore",
        return_value=store,
    ):
        assert _task_github_token(MagicMock(), task) == "task-token"
    assert "secret-value-should-not-appear" not in caplog.text
    assert all(call.kwargs["task_id"] == "matched-task" for call in store.get_record.call_args_list)


def test_legacy_task_credential_takes_precedence_over_environment(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "environment-token")
    task = MagicMock(secrets={"GITHUB_TOKEN": "task-token"})
    assert _task_github_token(MagicMock(), task) == "task-token"
