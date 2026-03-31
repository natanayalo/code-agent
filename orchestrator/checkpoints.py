"""Checkpoint helpers for durable orchestrator graph execution."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver


def create_in_memory_checkpointer() -> InMemorySaver:
    """Create an in-memory checkpointer for ephemeral graph execution."""

    return InMemorySaver()


@contextmanager
def create_sqlite_checkpointer(checkpoint_path: str | Path) -> Iterator[BaseCheckpointSaver]:
    """Create a SQLite-backed checkpointer for durable graph execution."""

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only in misconfigured envs
        raise RuntimeError(
            "SQLite checkpoint persistence requires the "
            "`langgraph-checkpoint-sqlite` package to be installed."
        ) from exc

    resolved_path = Path(checkpoint_path).expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    with SqliteSaver.from_conn_string(str(resolved_path)) as checkpointer:
        yield checkpointer
