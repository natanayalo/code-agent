"""Episodic observation service boundaries and logic."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.base import utc_now
from db.models import MemoryAdmissionDecision, MemoryObservation
from memory.admission import CustomMemoryAdmissionService, MemoryCandidate
from orchestrator.state import ObservationContextEntry
from privacy.redaction import redact_private_tags, redact_private_tags_recursive
from repositories import ObservationRepository

logger = logging.getLogger(__name__)


def strip_private_tags(text: str) -> tuple[str, bool]:
    """Strip case-insensitive <private>...</private> blocks and replace with [redacted-private]."""
    return redact_private_tags(text)


def strip_private_tags_recursive(data: Any) -> tuple[Any, bool]:
    """Recursively traverse dictionaries and lists, redacting private tags in strings."""
    return redact_private_tags_recursive(data)


class ObservationCaptureService:
    """Capture raw orchestrator, worker, and interaction events as episodic observations."""

    @staticmethod
    def capture_worker_run(
        session: Session,
        task: Any,
        worker_run: Any,
        result: Any,
    ) -> MemoryObservation:
        """Capture worker execution details upon completion (does not require admission)."""
        summary_text = result.summary or f"Worker finished with status {result.status}."
        files_str = ", ".join(result.files_changed) if result.files_changed else "(none)"
        run_id = worker_run.id if worker_run else "unknown"
        worker_type = (worker_run.worker_type or "unknown") if worker_run else "unknown"
        content_lines = [
            f"Worker Run ID: {run_id}",
            f"Worker Type: {worker_type}",
            f"Status: {result.status}",
            f"Files Changed: {files_str}",
        ]
        if result.commands_run:
            content_lines.append("Commands executed:")
            for cmd in result.commands_run:
                content_lines.append(f"  - [{cmd.exit_code}] {cmd.command}")

        content_text = "\n".join(content_lines)

        cmds_payload = []
        if result.commands_run:
            cmds_payload = [cmd.model_dump() for cmd in result.commands_run]

        mems_payload = []
        if result.memory_to_persist:
            mems_payload = [m.model_dump() for m in result.memory_to_persist]

        metadata = {
            "worker_run_id": worker_run.id if worker_run else None,
            "commands_run": cmds_payload,
            "files_changed": result.files_changed or [],
            "worker_memory_requests": mems_payload,
        }

        # Apply recursive redaction
        summary_text, s_stripped = strip_private_tags(summary_text)
        content_text, c_stripped = strip_private_tags(content_text)
        metadata, m_stripped = strip_private_tags_recursive(metadata)
        privacy_stripped = s_stripped or c_stripped or m_stripped

        repo_url = task.repo_url if task else None

        return ObservationRepository(session).create(
            task_id=task.id if task else None,
            session_id=task.session_id if task else None,
            repo_url=repo_url,
            worker_type=worker_run.worker_type if worker_run else None,
            source="worker",
            event_type="worker_completed" if result.status == "success" else "worker_failed",
            observed_at=utc_now(),
            summary=summary_text,
            content=content_text,
            metadata_payload=metadata,
            privacy_stripped=privacy_stripped,
            admission_status="not_required",
        )

    @staticmethod
    def capture_task_finalization(
        session: Session,
        task: Any,
        state: Any,
    ) -> MemoryObservation:
        """Capture task finalization details (completion/failure)."""
        status_val = task.status.value if task and task.status else "unknown"
        task_text = task.task_text if task else "unknown"
        summary_text = f"Task finalized with status {status_val}."
        content_text = f"Task objective: {task_text}\nFinal status in DB: {status_val}"

        metadata = {"final_status": status_val}

        summary_text, s_stripped = strip_private_tags(summary_text)
        content_text, c_stripped = strip_private_tags(content_text)
        metadata, m_stripped = strip_private_tags_recursive(metadata)
        privacy_stripped = s_stripped or c_stripped or m_stripped

        repo_url = task.repo_url if task else None

        return ObservationRepository(session).create(
            task_id=task.id if task else None,
            session_id=task.session_id if task else None,
            repo_url=repo_url,
            source="orchestrator",
            event_type="task_finalized",
            observed_at=utc_now(),
            summary=summary_text,
            content=content_text,
            metadata_payload=metadata,
            privacy_stripped=privacy_stripped,
            admission_status="not_required",
        )

    @staticmethod
    def capture_interaction_resolution(
        session: Session,
        task: Any,
        interaction: Any,
    ) -> MemoryObservation:
        """Capture human-in-the-loop decision card resolutions."""
        if hasattr(interaction.interaction_type, "value"):
            interaction_type = interaction.interaction_type.value
        else:
            interaction_type = str(interaction.interaction_type)
        summary_text = f"Interaction '{interaction_type}' resolved: {interaction.summary or ''}"
        content_text = (
            f"Resolution Status: {interaction.status}\n"
            f"Response data: {interaction.response_data}"
        )

        metadata = {
            "interaction_id": interaction.id,
            "interaction_type": interaction_type,
            "response_data": interaction.response_data or {},
        }

        summary_text, s_stripped = strip_private_tags(summary_text)
        content_text, c_stripped = strip_private_tags(content_text)
        metadata, m_stripped = strip_private_tags_recursive(metadata)
        privacy_stripped = s_stripped or c_stripped or m_stripped

        repo_url = task.repo_url if task else None

        return ObservationRepository(session).create(
            task_id=task.id if task else None,
            session_id=task.session_id if task else None,
            repo_url=repo_url,
            source="operator",
            event_type="interaction_resolved",
            observed_at=utc_now(),
            summary=summary_text,
            content=content_text,
            metadata_payload=metadata,
            privacy_stripped=privacy_stripped,
            admission_status="not_required",
        )


class ObservationContextService:
    """Build compact recent-context blocks from memory observations."""

    @staticmethod
    def build_recent_context_block(
        session: Session,
        repo_url: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        limit: int = 10,
    ) -> list[ObservationContextEntry]:
        """Fetch recent observations and format them into DTO items."""
        if repo_url is None and session_id is None and task_id is None:
            return []

        observations = ObservationRepository(session).recent(
            repo_url=repo_url,
            session_id=session_id,
            task_id=task_id,
            limit=limit,
        )

        entries = []
        for obs in observations:
            entries.append(
                ObservationContextEntry(
                    id=obs.id,
                    observed_at=obs.observed_at,
                    source=obs.source,
                    event_type=obs.event_type,
                    summary=obs.summary,
                    privacy_stripped=obs.privacy_stripped,
                )
            )
        return entries


def _check_already_processed(
    session: Session, obs: MemoryObservation, obs_repo: ObservationRepository
) -> bool:
    """Check if the observation has already been processed in decisions table."""
    decision_statement = select(MemoryAdmissionDecision).where(
        MemoryAdmissionDecision.source_observation_id == obs.id
    )
    existing_decision = session.scalar(decision_statement)
    if existing_decision is not None:
        obs_repo.update_admission_outcome(
            obs.id,
            status="processed",
            processed_at=utc_now(),
        )
        return True
    return False


def _parse_and_validate_candidate(
    obs: MemoryObservation, obs_repo: ObservationRepository
) -> MemoryCandidate | None:
    """Parse and validate MemoryCandidate from observation metadata."""
    candidate_dict = obs.metadata_payload.get("memory_candidate")
    if not isinstance(candidate_dict, dict):
        logger.warning(
            "Skipping observation %s: missing memory_candidate payload",
            obs.id,
        )
        obs_repo.update_admission_outcome(
            obs.id,
            status="invalid",
            processed_at=utc_now(),
            error="Missing memory_candidate key in metadata_payload.",
        )
        return None

    try:
        return MemoryCandidate(**candidate_dict)
    except Exception as val_exc:
        err_msg = f"Validation failed: {str(val_exc)[:200]}"
        logger.warning(
            "Skipping observation %s: invalid memory_candidate structure",
            obs.id,
        )
        obs_repo.update_admission_outcome(
            obs.id,
            status="invalid",
            processed_at=utc_now(),
            error=err_msg,
        )
        return None


def _attach_observation_lineage(candidate: MemoryCandidate, obs: MemoryObservation) -> None:
    """Backfill candidate provenance from the source observation when omitted."""
    candidate.source_observation_id = obs.id
    if not candidate.task_id:
        candidate.task_id = obs.task_id
    if not candidate.session_id:
        candidate.session_id = obs.session_id
    category_str = getattr(candidate.category, "value", candidate.category)
    if category_str == "project" and not candidate.repo_url:
        candidate.repo_url = obs.repo_url


def _process_single_observation(
    session: Session, obs: MemoryObservation, obs_repo: ObservationRepository
) -> None:
    """Process a single observation inside its own savepoint transaction."""
    with session.begin_nested():
        if _check_already_processed(session, obs, obs_repo):
            return

        candidate = _parse_and_validate_candidate(obs, obs_repo)
        if candidate is None:
            return

        _attach_observation_lineage(candidate, obs)

        # Submit to Memory Admission Service
        admission_service = CustomMemoryAdmissionService(session)
        admission_service.admit_candidates(candidates=[candidate])

        # Update status
        obs_repo.update_admission_outcome(
            obs.id,
            status="processed",
            processed_at=utc_now(),
        )


class ObservationMemoryBridge:
    """Synchronous, local bridge to promote pending observations to memory candidates."""

    @staticmethod
    def bridge_observations(session: Session, task_id: str) -> None:
        """Fetch pending observations for a task, validate candidate payloads, and run admission."""
        obs_repo = ObservationRepository(session)
        statement = select(MemoryObservation).where(
            MemoryObservation.task_id == task_id,
            MemoryObservation.admission_status == "pending",
        )
        pending_obs = list(session.scalars(statement))
        if not pending_obs:
            return

        for obs in pending_obs:
            try:
                _process_single_observation(session, obs, obs_repo)
            except Exception as exc:
                # Isolate bridge failures: catch, log, and mark status as failed
                err_text = f"Bridge processing failed: {str(exc)[:500]}"
                logger.error(
                    "ObservationMemoryBridge failed to process observation %s: %s",
                    obs.id,
                    exc,
                    exc_info=True,
                )
                try:
                    obs_repo.update_admission_outcome(
                        obs.id,
                        status="failed",
                        processed_at=utc_now(),
                        error=err_text,
                    )
                except Exception as nested_exc:
                    logger.error(
                        "Failed to mark observation %s as failed: %s",
                        obs.id,
                        nested_exc,
                    )
