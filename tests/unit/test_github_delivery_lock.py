"""Delivery lock keys must agree across URL forms and processes."""

from unittest.mock import MagicMock

from orchestrator.github_delivery_sync import _lock_delivery_updates


def test_postgres_delivery_lock_uses_canonical_repo_key():
    session = MagicMock()
    session.get_bind.return_value.dialect.name = "postgresql"
    session.scalar.return_value = True
    assert _lock_delivery_updates(session, "https://github.com/Octocat/project.git")
    first_statement, first_parameters = session.scalar.call_args.args
    _lock_delivery_updates(session, "https://github.com/octocat/project")
    second_statement, second_parameters = session.scalar.call_args.args
    assert str(first_statement) == "SELECT pg_try_advisory_xact_lock(:lock_id)"
    assert str(second_statement) == str(first_statement)
    assert first_parameters == second_parameters
    _lock_delivery_updates(session, "https://github.com/octocat/other")
    assert session.scalar.call_args.args[1] != first_parameters
    session.scalar.return_value = False
    assert not _lock_delivery_updates(session, "https://github.com/octocat/project")
