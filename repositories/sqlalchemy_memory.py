"""Skeptical-memory SQLAlchemy repositories."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import PersonalMemory, ProjectMemory
from repositories.sqlalchemy_common import UNSET, apply_memory_metadata

_SEARCH_LIMIT_CAP = 100
_SEARCH_QUERY_MAX_CHARS = 200
_RELAXED_TOKEN_LIMIT = 8
_RELAXED_TOKEN_MIN_LENGTH = 3
_RELAXED_SEARCH_STOP_WORDS = frozenset(
    {
        "and",
        "are",
        "but",
        "for",
        "from",
        "into",
        "not",
        "the",
        "this",
        "that",
        "then",
        "with",
        "after",
        "before",
        "change",
        "create",
        "exactly",
        "future",
        "tasks",
    }
)
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


@dataclass(frozen=True)
class MemorySearchResult:
    """A memory row paired with an optional operator-facing search snippet."""

    memory: PersonalMemory | ProjectMemory
    headline: str | None = None


def _normalized_search_limit(limit: int) -> int:
    return max(1, min(limit, _SEARCH_LIMIT_CAP))


def _normalized_search_query(query: str) -> str:
    return query[:_SEARCH_QUERY_MAX_CHARS].strip()


def _relaxed_tsquery(query: str) -> str | None:
    """Build a conservative OR tsquery fallback from significant alphanumeric tokens."""
    tokens: list[str] = []
    for raw_token in re.findall(r"\w+", query.casefold()):
        if len(raw_token) < _RELAXED_TOKEN_MIN_LENGTH:
            continue
        if raw_token in _RELAXED_SEARCH_STOP_WORDS:
            continue
        if raw_token in tokens:
            continue
        tokens.append(raw_token)
        if len(tokens) >= _RELAXED_TOKEN_LIMIT:
            break
    if not tokens:
        return None
    return " | ".join(tokens)


def _dialect_name(session: Session) -> str:
    try:
        bind = session.get_bind()
    except Exception:
        return ""
    return bind.dialect.name if bind is not None else ""


def _fallback_search_results(
    memories: Sequence[PersonalMemory | ProjectMemory],
    *,
    query: str,
    limit: int,
) -> list[MemorySearchResult]:
    lowered_query = query.casefold()
    results: list[MemorySearchResult] = []
    for memory in memories:
        memory_value = str(memory.value or "").casefold()
        if lowered_query in memory.memory_key.casefold() or lowered_query in memory_value:
            results.append(MemorySearchResult(memory=memory))
            if len(results) >= limit:
                break
    return results


def _ordered_search_results(
    session: Session,
    *,
    statement: str,
    params: dict[str, Any],
    model: type[PersonalMemory] | type[ProjectMemory],
) -> list[MemorySearchResult]:
    rows = session.execute(text(statement), params).mappings().all()
    if not rows:
        return []

    memory_ids = [row["id"] for row in rows]
    model_id = cast(Any, model).id
    memories = cast(
        list[PersonalMemory | ProjectMemory],
        session.scalars(select(model).where(model_id.in_(memory_ids))).all(),
    )
    memories_by_id = {str(memory.id): memory for memory in memories}

    ordered_results: list[MemorySearchResult] = []
    for row in rows:
        memory = memories_by_id.get(str(row["id"]))
        if memory is None:
            continue
        headline = row.get("headline")
        ordered_results.append(
            MemorySearchResult(
                memory=memory,
                headline=headline if isinstance(headline, str) else None,
            )
        )
    return ordered_results


def _postgres_search_with_relaxed_fallback(
    session: Session,
    *,
    strict_statement: str,
    relaxed_statement: str,
    base_params: dict[str, Any],
    query: str,
    limit: int,
    model: type[PersonalMemory] | type[ProjectMemory],
) -> list[MemorySearchResult]:
    params = {
        **base_params,
        "query": query,
        "limit": _normalized_search_limit(limit),
        "headline_options": _HEADLINE_OPTIONS,
    }
    strict_results = _ordered_search_results(
        session,
        statement=strict_statement,
        params=params,
        model=model,
    )
    if strict_results:
        return strict_results

    relaxed_query = _relaxed_tsquery(query)
    if relaxed_query is None:
        return []
    return _ordered_search_results(
        session,
        statement=relaxed_statement,
        params={**params, "query": relaxed_query},
        model=model,
    )


class PersonalMemoryRepository:
    """Persist and query personal memory entries."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, *, memory_key: str) -> PersonalMemory | None:
        statement = select(PersonalMemory).where(PersonalMemory.memory_key == memory_key)
        return self.session.scalar(statement)

    def list_all(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PersonalMemory]:
        statement = (
            select(PersonalMemory)
            .order_by(PersonalMemory.created_at.desc(), PersonalMemory.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(statement))

    def count_all(self) -> tuple[int, int]:
        """Return total and requires-verification counts for personal memory."""
        total_statement = select(func.count()).select_from(PersonalMemory)
        verification_statement = total_statement.where(
            PersonalMemory.requires_verification.is_(True)
        )
        total = self.session.scalar(total_statement) or 0
        requires_verification = self.session.scalar(verification_statement) or 0
        return int(total), int(requires_verification)

    def search(
        self,
        *,
        query: str,
        limit: int = 20,
    ) -> list[MemorySearchResult]:
        """Search personal memory entries, falling back to substring matching outside Postgres."""
        normalized_query = _normalized_search_query(query)
        if not normalized_query:
            return []

        if _dialect_name(self.session) != "postgresql":
            return _fallback_search_results(
                self.list_all(),
                query=normalized_query,
                limit=_normalized_search_limit(limit),
            )

        return _postgres_search_with_relaxed_fallback(
            self.session,
            strict_statement="""
                SELECT
                    id,
                    ts_headline(
                        'english',
                        coalesce(memory_key, '') || ' ' || coalesce(value::text, ''),
                        plainto_tsquery('english', :query),
                        :headline_options
                    ) AS headline
                FROM memory_personal
                WHERE search_vector @@ plainto_tsquery('english', :query)
                ORDER BY
                  ts_rank(search_vector, plainto_tsquery('english', :query)) DESC,
                  created_at DESC,
                  id DESC
                LIMIT :limit
            """,
            relaxed_statement="""
                SELECT
                    id,
                    ts_headline(
                        'english',
                        coalesce(memory_key, '') || ' ' || coalesce(value::text, ''),
                        to_tsquery('english', :query),
                        :headline_options
                    ) AS headline
                FROM memory_personal
                WHERE search_vector @@ to_tsquery('english', :query)
                ORDER BY
                  ts_rank(search_vector, to_tsquery('english', :query)) DESC,
                  created_at DESC,
                  id DESC
                LIMIT :limit
            """,
            base_params={},
            query=normalized_query,
            limit=limit,
            model=PersonalMemory,
        )

    def upsert(
        self,
        *,
        memory_key: str,
        value: dict[str, Any],
        source: str | None | object = UNSET,
        confidence: float | object = UNSET,
        scope: str | None | object = UNSET,
        last_verified_at: datetime | None | object = UNSET,
        requires_verification: bool | object = UNSET,
    ) -> PersonalMemory:
        memory_entry = self.get(memory_key=memory_key)
        if memory_entry is None:
            memory_entry = PersonalMemory(
                memory_key=memory_key,
                value=value,
            )
            try:
                with self.session.begin_nested():
                    self.session.add(memory_entry)
                    self.session.flush()
            except IntegrityError:
                memory_entry = self.get(memory_key=memory_key)
                if memory_entry is None:
                    raise
        apply_memory_metadata(
            memory_entry,
            value=value,
            source=source,
            confidence=confidence,
            scope=scope,
            last_verified_at=last_verified_at,
            requires_verification=requires_verification,
        )
        self.session.flush()
        return memory_entry

    def delete(self, *, memory_key: str) -> bool:
        memory_entry = self.get(memory_key=memory_key)
        if memory_entry is None:
            return False
        self.session.delete(memory_entry)
        self.session.flush()
        return True


class ProjectMemoryRepository:
    """Persist and query project memory entries."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, *, repo_url: str, memory_key: str) -> ProjectMemory | None:
        statement = select(ProjectMemory).where(
            ProjectMemory.repo_url == repo_url,
            ProjectMemory.memory_key == memory_key,
        )
        return self.session.scalar(statement)

    def list_by_repo(self, repo_url: str) -> list[ProjectMemory]:
        statement = (
            select(ProjectMemory)
            .where(ProjectMemory.repo_url == repo_url)
            .order_by(ProjectMemory.created_at.desc(), ProjectMemory.id.desc())
        )
        return list(self.session.scalars(statement))

    def count_all(self, repo_url: str | None = None) -> tuple[int, int]:
        """Return total and requires-verification counts for project memory."""
        total_statement = select(func.count()).select_from(ProjectMemory)
        if repo_url is not None:
            total_statement = total_statement.where(ProjectMemory.repo_url == repo_url)
        verification_statement = total_statement.where(
            ProjectMemory.requires_verification.is_(True)
        )
        total = self.session.scalar(total_statement) or 0
        requires_verification = self.session.scalar(verification_statement) or 0
        return int(total), int(requires_verification)

    def search(
        self,
        *,
        repo_url: str,
        query: str,
        limit: int = 20,
    ) -> list[MemorySearchResult]:
        """Search project memory entries, falling back to substring matching outside Postgres."""
        normalized_query = _normalized_search_query(query)
        if not normalized_query:
            return []

        if _dialect_name(self.session) != "postgresql":
            return _fallback_search_results(
                self.list_by_repo(repo_url),
                query=normalized_query,
                limit=_normalized_search_limit(limit),
            )

        return _postgres_search_with_relaxed_fallback(
            self.session,
            strict_statement="""
                SELECT
                    id,
                    ts_headline(
                        'english',
                        coalesce(memory_key, '') || ' ' || coalesce(value::text, ''),
                        plainto_tsquery('english', :query),
                        :headline_options
                    ) AS headline
                FROM memory_project
                WHERE repo_url = :repo_url
                  AND search_vector @@ plainto_tsquery('english', :query)
                ORDER BY
                  ts_rank(search_vector, plainto_tsquery('english', :query)) DESC,
                  created_at DESC,
                  id DESC
                LIMIT :limit
            """,
            relaxed_statement="""
                SELECT
                    id,
                    ts_headline(
                        'english',
                        coalesce(memory_key, '') || ' ' || coalesce(value::text, ''),
                        to_tsquery('english', :query),
                        :headline_options
                    ) AS headline
                FROM memory_project
                WHERE repo_url = :repo_url
                  AND search_vector @@ to_tsquery('english', :query)
                ORDER BY
                  ts_rank(search_vector, to_tsquery('english', :query)) DESC,
                  created_at DESC,
                  id DESC
                LIMIT :limit
            """,
            base_params={"repo_url": repo_url},
            query=normalized_query,
            limit=limit,
            model=ProjectMemory,
        )

    def list_all(
        self,
        *,
        repo_url: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProjectMemory]:
        statement = select(ProjectMemory)
        if repo_url is not None:
            statement = statement.where(ProjectMemory.repo_url == repo_url)
        statement = (
            statement.order_by(ProjectMemory.created_at.desc(), ProjectMemory.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(statement))

    def upsert(
        self,
        *,
        repo_url: str,
        memory_key: str,
        value: dict[str, Any],
        source: str | None | object = UNSET,
        confidence: float | object = UNSET,
        scope: str | None | object = UNSET,
        last_verified_at: datetime | None | object = UNSET,
        requires_verification: bool | object = UNSET,
    ) -> ProjectMemory:
        memory_entry = self.get(repo_url=repo_url, memory_key=memory_key)
        if memory_entry is None:
            memory_entry = ProjectMemory(
                repo_url=repo_url,
                memory_key=memory_key,
                value=value,
            )
            try:
                with self.session.begin_nested():
                    self.session.add(memory_entry)
                    self.session.flush()
            except IntegrityError:
                memory_entry = self.get(repo_url=repo_url, memory_key=memory_key)
                if memory_entry is None:
                    raise
        apply_memory_metadata(
            memory_entry,
            value=value,
            source=source,
            confidence=confidence,
            scope=scope,
            last_verified_at=last_verified_at,
            requires_verification=requires_verification,
        )
        self.session.flush()
        return memory_entry

    def delete(self, *, repo_url: str, memory_key: str) -> bool:
        memory_entry = self.get(repo_url=repo_url, memory_key=memory_key)
        if memory_entry is None:
            return False
        self.session.delete(memory_entry)
        self.session.flush()
        return True
