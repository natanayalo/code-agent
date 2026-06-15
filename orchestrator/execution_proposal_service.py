"""Proposal management helpers for the execution service."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from sqlalchemy import update

from db.enums import ProposalStatus
from db.models import Proposal
from orchestrator.execution_types import (
    DeliveryKey,
    ProposalSnapshot,
    SubmissionSession,
    TaskSnapshot,
    TaskSubmission,
)
from repositories import (
    ProposalRepository,
    SessionRepository,
    TaskRepository,
    UserRepository,
    session_scope,
)

logger = logging.getLogger("orchestrator.execution.proposals")


def _map_proposal_to_snapshot(proposal: Proposal) -> ProposalSnapshot:
    """Convert a Proposal SQLAlchemy model to a ProposalSnapshot."""
    return ProposalSnapshot(
        proposal_id=proposal.id,
        session_id=proposal.session_id,
        task_id=proposal.task_id,
        title=proposal.title,
        summary=proposal.summary,
        content=proposal.content,
        status=proposal.status.value if hasattr(proposal.status, "value") else str(proposal.status),
        proposal_type=(
            proposal.proposal_type.value
            if hasattr(proposal.proposal_type, "value")
            else str(proposal.proposal_type)
        ),
        metadata_payload=dict(proposal.metadata_payload) if proposal.metadata_payload else {},
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
    )


def list_proposals(
    self: Any,
    *,
    status: ProposalStatus | str | None = None,
    proposal_type: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ProposalSnapshot]:
    """List proposals, optionally filtering by status, session_id, or task_id."""
    with session_scope(self.session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            status=status,
            proposal_type=proposal_type,
            session_id=session_id,
            task_id=task_id,
            limit=limit,
            offset=offset,
        )
        return [_map_proposal_to_snapshot(p) for p in proposals]


def _build_task_text_for_proposal(proposal: Proposal) -> str:
    task_text = f"Proposal: {proposal.title}\n\n{proposal.summary}"
    if proposal.content:
        task_text += f"\n\nDetails:\n{proposal.content}"

    metadata = proposal.metadata_payload or {}
    if "diff_text" in metadata:
        task_text += f"\n\nDiff:\n```diff\n{metadata['diff_text']}\n```"
    files_changed = metadata.get("files_changed")
    if isinstance(files_changed, list):
        files_joined = "\n".join(str(f) for f in files_changed if f)
        task_text += f"\n\nFiles changed:\n{files_joined}"
    if "json_payload" in metadata:
        payload_str = json.dumps(metadata["json_payload"], indent=2)
        task_text += f"\n\nJSON Payload:\n```json\n{payload_str}\n```"

    return task_text


def accept_proposal(
    self: Any, proposal_id: str
) -> tuple[
    Literal["created", "conflict", "not_found"],
    TaskSnapshot | None,
    str | None,
]:
    """
    Accept a proposal. Promotes it to a real task in the primary lane.
    If the proposal is already accepted, returns the existing task (idempotent).
    Returns (status, task_snapshot, detail).
    """
    with session_scope(self.session_factory) as session:
        repo = ProposalRepository(session)
        try:
            proposal = repo.get_proposal(proposal_id)
        except ValueError:
            return "not_found", None, f"Proposal '{proposal_id}' was not found."

        if proposal.status == ProposalStatus.ACCEPTED:
            # Idempotent return
            accepted_task_id = (proposal.metadata_payload or {}).get("accepted_task_id")
            if accepted_task_id:
                task_snapshot = self.get_task(accepted_task_id)
                if task_snapshot:
                    return "conflict", task_snapshot, "Proposal is already accepted."
            return "conflict", None, "Proposal is already accepted but missing accepted_task_id."

        if proposal.status != ProposalStatus.PENDING_REVIEW:
            return "conflict", None, f"Proposal cannot be accepted from status '{proposal.status}'."

        # Prepare to create task
        session_repo = SessionRepository(session)
        user_repo = UserRepository(session)
        task_repo = TaskRepository(session)

        conversation_session = session_repo.get(proposal.session_id)
        if not conversation_session:
            return "conflict", None, "Session for proposal not found."

        user = user_repo.get(conversation_session.user_id)
        if not user:
            return "conflict", None, "User for proposal session not found."

        # Retrieve source task context if available
        repo_url = None
        branch = None
        if proposal.task_id:
            source_task = task_repo.get(proposal.task_id)
            if source_task:
                repo_url = source_task.repo_url
                branch = source_task.branch

        # Construct task text from proposal
        task_text = _build_task_text_for_proposal(proposal)

        # Extract primitive values before exiting session scope to avoid DetachedInstanceError
        channel = conversation_session.channel
        external_thread_id = conversation_session.external_thread_id
        external_user_id = user.external_user_id or "unknown"
        display_name = user.display_name

    # We must call create_task_outcome outside the session_scope
    # because it creates its own session_scope.
    submission = TaskSubmission(
        task_text=task_text,
        repo_url=repo_url,
        branch=branch,
        priority=0,
        session=SubmissionSession(
            channel=channel,
            external_user_id=external_user_id,
            external_thread_id=external_thread_id,
            display_name=display_name,
        ),
        # Strip scout constraints by not copying them from source task
    )

    # Use a DeliveryKey based on proposal_id to ensure idempotency at the task creation level too
    delivery_key = DeliveryKey(
        channel=channel,
        delivery_id=f"proposal_{proposal_id}",
    )

    outcome = self.create_task_outcome(submission, delivery_key=delivery_key)

    try:
        with session_scope(self.session_factory) as session:
            repo = ProposalRepository(session)
            proposal = repo.get_proposal(proposal_id)

            metadata = dict(proposal.metadata_payload) if proposal.metadata_payload else {}
            metadata["accepted_task_id"] = outcome.task_snapshot.task_id

            stmt = (
                update(Proposal)
                .where(Proposal.id == proposal_id, Proposal.status == ProposalStatus.PENDING_REVIEW)
                .values(status=ProposalStatus.ACCEPTED, metadata_payload=metadata)
            )
            result = session.execute(stmt)

            if getattr(result, "rowcount", 0) == 0:
                session.expire(proposal)
                refetched = session.get(Proposal, proposal_id)
                if refetched is None:
                    if not outcome.duplicate:
                        self.cancel_task(task_id=outcome.task_snapshot.task_id)
                    return "not_found", None, f"Proposal '{proposal_id}' was deleted concurrently."
                if refetched.status == ProposalStatus.ACCEPTED:
                    accepted_task_id = (refetched.metadata_payload or {}).get("accepted_task_id")
                    if accepted_task_id and accepted_task_id != outcome.task_snapshot.task_id:
                        # Cancel our orphaned task since another task was accepted for this proposal
                        if not outcome.duplicate:
                            self.cancel_task(task_id=outcome.task_snapshot.task_id)
                        actual_task = self.get_task(accepted_task_id)
                        return (
                            "conflict",
                            actual_task,
                            "Proposal was accepted concurrently with a different task.",
                        )
                    return "conflict", outcome.task_snapshot, "Proposal was accepted concurrently."

                if not outcome.duplicate:
                    self.cancel_task(task_id=outcome.task_snapshot.task_id)
                return "conflict", None, "Proposal was modified concurrently."

            session.expire(proposal)
    except Exception:
        try:
            if not outcome.duplicate:
                self.cancel_task(task_id=outcome.task_snapshot.task_id)
        except Exception as cancel_err:
            logger.error(
                "Failed to cancel orphaned task %s after proposal update error: %s",
                outcome.task_snapshot.task_id,
                cancel_err,
            )
        raise

    return "created", outcome.task_snapshot, None


def reject_proposal(
    self: Any, proposal_id: str
) -> tuple[
    Literal["success", "conflict", "not_found"],
    ProposalSnapshot | None,
    str | None,
]:
    """
    Reject a proposal. Updates its status to REJECTED.
    Returns (status, proposal_snapshot, detail).
    """
    with session_scope(self.session_factory) as session:
        repo = ProposalRepository(session)
        try:
            proposal = repo.get_proposal(proposal_id)
        except ValueError:
            return "not_found", None, f"Proposal '{proposal_id}' was not found."

        if proposal.status == ProposalStatus.REJECTED:
            return "success", _map_proposal_to_snapshot(proposal), None

        if proposal.status != ProposalStatus.PENDING_REVIEW:
            return "conflict", None, f"Proposal cannot be rejected from status '{proposal.status}'."

        stmt = (
            update(Proposal)
            .where(Proposal.id == proposal_id, Proposal.status == ProposalStatus.PENDING_REVIEW)
            .values(status=ProposalStatus.REJECTED)
        )
        result = session.execute(stmt)
        if getattr(result, "rowcount", 0) == 0:
            session.expire(proposal)
            refetched = session.get(Proposal, proposal_id)
            if refetched is None:
                return "not_found", None, f"Proposal '{proposal_id}' was deleted concurrently."
            if refetched.status == ProposalStatus.REJECTED:
                return "success", _map_proposal_to_snapshot(refetched), None
            return "conflict", None, "Proposal was modified concurrently."

        session.expire(proposal)
        return "success", _map_proposal_to_snapshot(proposal), None
