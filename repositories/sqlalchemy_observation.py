"""SQLAlchemy-backed repository for memory observations."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from db.base import utc_now
from db.models import MemoryObservation

_SEARCH_LIMIT_CAP = 100
_SEARCH_QUERY_MAX_CHARS = 200
_HEADLINE_START = "__CA_MARK_START__"
_HEADLINE_END = "__CA_MARK_END__"
_HEADLINE_OPTIONS = (
    f"StartSel={_HEADLINE_START},"
    f"StopSel={_HEADLINE_END},"
    "MaxFragments=2,"
    "MaxWords=18,"
    "MinWords=6,"
    "FragmentDelimiter= ... "
)


def _normalized_search_limit(limit: int) -> int:
    return max(1, min(limit, _SEARCH_LIMIT_CAP))


def _normalized_search_query(query: str) -> str:
    return query[:_SEARCH_QUERY_MAX_CHARS].strip()


def _dialect_name(session: Session) -> str:
    try:
        bind = session.get_bind()
    except Exception:
        return ""
    return bind.dialect.name if bind is not None else ""


class ObservationRepository:
    """Persist and query episodic task and session observations."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        source: str,
        event_type: str,
        summary: str,
        content: str,
        task_id: str | None = None,
        session_id: str | None = None,
        repo_url: str | None = None,
        worker_type: str | None = None,
        observed_at: datetime | None = None,
        metadata_payload: dict[str, Any] | None = None,
        privacy_stripped: bool = False,
        admission_status: str = "not_required",
        admission_processed_at: datetime | None = None,
        admission_error: str | None = None,
    ) -> MemoryObservation:
        """Create and persist a new memory observation."""
        obs = MemoryObservation(
            id=str(uuid.uuid4()),
            task_id=task_id,
            session_id=session_id,
            repo_url=repo_url,
            worker_type=worker_type,
            source=source,
            event_type=event_type,
            observed_at=observed_at or utc_now(),
            summary=summary,
            content=content,
            metadata_payload=metadata_payload or {},
            privacy_stripped=privacy_stripped,
            admission_status=admission_status,
            admission_processed_at=admission_processed_at,
            admission_error=admission_error,
        )
        self.session.add(obs)
        return obs

    def get(self, observation_id: str) -> MemoryObservation | None:
        """Get an observation by its primary ID."""
        statement = select(MemoryObservation).where(MemoryObservation.id == observation_id)
        return self.session.scalar(statement)

    def list_timeline(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> list[MemoryObservation]:
        """List observations ordered by observed_at ascending.

        At least one scope parameter is required.
        """
        if session_id is None and task_id is None:
            raise ValueError("list_timeline requires at least one of session_id or task_id.")

        statement = select(MemoryObservation)
        if session_id is not None:
            statement = statement.where(MemoryObservation.session_id == session_id)
        if task_id is not None:
            statement = statement.where(MemoryObservation.task_id == task_id)

        statement = statement.order_by(
            MemoryObservation.observed_at.asc(), MemoryObservation.id.asc()
        )
        return list(self.session.scalars(statement))

    def _search_sqlite(
        self,
        query: str,
        repo_url: str | None,
        session_id: str | None,
        task_id: str | None,
        limit: int,
    ) -> list[MemoryObservation]:
        """Fallback substring search for SQLite."""
        statement = select(MemoryObservation)
        if repo_url is not None:
            statement = statement.where(MemoryObservation.repo_url == repo_url)
        if session_id is not None:
            statement = statement.where(MemoryObservation.session_id == session_id)
        if task_id is not None:
            statement = statement.where(MemoryObservation.task_id == task_id)

        statement = statement.where(
            or_(
                MemoryObservation.summary.ilike(f"%{query}%"),
                MemoryObservation.content.ilike(f"%{query}%"),
            )
        ).limit(limit)
        return list(self.session.scalars(statement))

    def _search_postgresql(
        self,
        query: str,
        repo_url: str | None,
        session_id: str | None,
        task_id: str | None,
        limit: int,
    ) -> list[MemoryObservation]:
        """Ranked full-text search for Postgres."""
        limit_val = _normalized_search_limit(limit)
        sql_statement = """
            SELECT id
            FROM memory_observations
            WHERE search_vector @@ plainto_tsquery('english', :query)
        """
        params: dict[str, Any] = {
            "query": query,
            "limit": limit_val,
        }

        if repo_url is not None:
            sql_statement += " AND repo_url = :repo_url"
            params["repo_url"] = repo_url
        if session_id is not None:
            sql_statement += " AND session_id = :session_id"
            params["session_id"] = session_id
        if task_id is not None:
            sql_statement += " AND task_id = :task_id"
            params["task_id"] = task_id

        sql_statement += """
            ORDER BY
              ts_rank(search_vector, plainto_tsquery('english', :query)) DESC,
              observed_at DESC,
              id DESC
            LIMIT :limit
        """

        rows = self.session.execute(text(sql_statement), params).mappings().all()
        if not rows:
            return []

        observation_ids = [row["id"] for row in rows]
        mem_obs = list(
            self.session.scalars(
                select(MemoryObservation).where(MemoryObservation.id.in_(observation_ids))
            ).all()
        )
        obs_by_id = {obs.id: obs for obs in mem_obs}
        ordered_results: list[MemoryObservation] = []
        for r in rows:
            obs_item = obs_by_id.get(r["id"])
            if obs_item is not None:
                ordered_results.append(obs_item)
        return ordered_results

    def search(
        self,
        *,
        query: str,
        repo_url: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        limit: int = 20,
    ) -> list[MemoryObservation]:
        """Search observations using Postgres full-text search, with fallback on SQLite."""
        if repo_url is None and session_id is None and task_id is None:
            raise ValueError(
                "search requires at least one scope (repo_url, session_id, or task_id)."
            )

        normalized_query = _normalized_search_query(query)
        if not normalized_query:
            return []

        dialect = _dialect_name(self.session)
        if dialect != "postgresql":
            return self._search_sqlite(normalized_query, repo_url, session_id, task_id, limit)

        return self._search_postgresql(normalized_query, repo_url, session_id, task_id, limit)

    def recent(
        self,
        *,
        repo_url: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        limit: int = 10,
    ) -> list[MemoryObservation]:
        """Fetch recent observations. At least one scope is required."""
        if repo_url is None and session_id is None and task_id is None:
            raise ValueError(
                "recent requires at least one scope (repo_url, session_id, or task_id)."
            )

        statement = select(MemoryObservation)
        if repo_url is not None:
            statement = statement.where(MemoryObservation.repo_url == repo_url)
        if session_id is not None:
            statement = statement.where(MemoryObservation.session_id == session_id)
        if task_id is not None:
            statement = statement.where(MemoryObservation.task_id == task_id)

        statement = statement.order_by(
            MemoryObservation.observed_at.desc(),
            MemoryObservation.id.desc(),
        ).limit(limit)
        return list(self.session.scalars(statement))

    def update_admission_outcome(
        self,
        observation_id: str,
        status: str,
        processed_at: datetime | None = None,
        error: str | None = None,
    ) -> None:
        """Update admission status and details for an observation."""
        statement = select(MemoryObservation).where(MemoryObservation.id == observation_id)
        obs = self.session.scalar(statement)
        if obs is not None:
            obs.admission_status = status
            obs.admission_processed_at = processed_at
            obs.admission_error = error
