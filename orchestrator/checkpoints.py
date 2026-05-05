"""Checkpoint helpers for durable orchestrator graph execution."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from importlib import import_module
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
        from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only in misconfigured envs
        raise RuntimeError(
            "SQLite checkpoint persistence requires the "
            "`langgraph-checkpoint-sqlite` package to be installed."
        ) from exc

    resolved_path = Path(checkpoint_path).expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    with SqliteSaver.from_conn_string(str(resolved_path)) as checkpointer:
        yield checkpointer


@asynccontextmanager
async def create_async_sqlite_checkpointer(
    checkpoint_path: str | Path,
) -> AsyncIterator[BaseCheckpointSaver]:
    """Create an async SQLite-backed checkpointer for async graph execution."""

    try:
        sqlite_aio_module = import_module("langgraph.checkpoint.sqlite.aio")
        AsyncSqliteSaver = sqlite_aio_module.AsyncSqliteSaver
    except ImportError as exc:  # pragma: no cover - exercised only in misconfigured envs
        raise RuntimeError(
            "Async SQLite checkpoint persistence requires the "
            "`langgraph-checkpoint-sqlite` package and `aiosqlite` to be installed."
        ) from exc

    resolved_path = Path(checkpoint_path).expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(str(resolved_path)) as checkpointer:
        yield checkpointer
