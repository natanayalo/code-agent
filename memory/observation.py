"""Episodic observation service boundaries and logic."""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.base import utc_now
from db.models import MemoryAdmissionDecision, MemoryObservation, MemoryProposal, Task
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


def _string_value(value: Any) -> str:
    """Return a stable string for raw strings and enum-like values."""
    return str(getattr(value, "value", value))


class ObservationCaptureService:
    """Capture raw orchestrator, worker, and interaction events as episodic observations."""

    @staticmethod
    def capture_worker_run(
        session: Session,
        task: Any,
        worker_run: Any,
        result: Any,
        verifier_outcome: dict[str, Any] | None = None,
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
            "verifier_outcome": verifier_outcome or {},
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
        status_val = _string_value(task.status) if task and task.status else "unknown"
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
        status_val = _string_value(interaction.status)
        content_text = (
            f"Resolution Status: {status_val}\nResponse data: {interaction.response_data}"
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
    def load_recent_context_entries(
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


def _bridge_summary(session: Session, task_id: str) -> dict[str, Any]:
    observations = list(
        session.scalars(select(MemoryObservation).where(MemoryObservation.task_id == task_id))
    )
    decisions = list(
        session.scalars(
            select(MemoryAdmissionDecision).where(MemoryAdmissionDecision.task_id == task_id)
        )
    )
    proposal_count = (
        session.scalar(
            select(func.count())
            .select_from(MemoryProposal)
            .where(MemoryProposal.task_id == task_id)
        )
        or 0
    )
    admission_status_counts = Counter(_string_value(obs.admission_status) for obs in observations)
    decision_counts = Counter(_string_value(decision.decision) for decision in decisions)
    risk_counts = Counter(_string_value(decision.risk_level) for decision in decisions)
    return {
        "observation_count": len(observations),
        "extracted_candidate_count": sum(
            1 for obs in observations if obs.event_type == "extracted_candidate"
        ),
        "pending_observation_count": admission_status_counts.get("pending", 0),
        "processed_observation_count": admission_status_counts.get("processed", 0),
        "failed_observation_count": admission_status_counts.get("failed", 0),
        "invalid_observation_count": admission_status_counts.get("invalid", 0),
        "decision_counts": dict(decision_counts),
        "risk_counts": dict(risk_counts),
        "proposal_count": int(proposal_count),
        "durable_memory_count": sum(
            1 for decision in decisions if decision.durable_memory_id is not None
        ),
    }


def _is_verification_command(command_str: str) -> bool:
    cmd = command_str.strip()
    prefixes = [
        "poetry run",
        ".venv/bin/",
        "python -m",
        "python3 -m",
        "bundle exec",
        "npx",
        "npm run",
        "npm",
        "yarn run",
        "yarn",
        "bun run",
        "bun",
    ]
    cleaned = cmd
    for p in prefixes:
        if cleaned.lower().startswith(p):
            cleaned = cleaned[len(p) :].strip()
    parts = cleaned.split()
    if not parts:
        return False
    exe = parts[0].lower()
    if exe in ("pytest", "tox", "rake", "vitest", "jest", "mocha"):
        return True
    if exe in ("go", "cargo") and len(parts) > 1 and parts[1].lower() == "test":
        return True
    if exe in ("python", "python3", "node") and len(parts) > 1:
        for arg in parts[1:]:
            arg_l = arg.lower()
            if "test_" in arg_l or "_test" in arg_l or arg_l.startswith("test"):
                return True
    if exe == "test" or (exe == "run" and len(parts) > 1 and parts[1].lower() == "test"):
        return True
    if exe == "make" and len(parts) > 1 and parts[1].lower() in ("test", "ci"):
        return True
    return False


def _get_base_executable(cmd_str: str) -> str:
    cmd = cmd_str.strip()
    prefixes = [
        "poetry run",
        ".venv/bin/",
        "python -m",
        "python3 -m",
        "bundle exec",
        "npx",
        "npm run",
        "npm",
    ]
    cleaned = cmd
    for p in prefixes:
        if cleaned.lower().startswith(p):
            cleaned = cleaned[len(p) :].strip()
    parts = cleaned.split()
    return parts[0].lower() if parts else ""


def _extract_remember_sentences(text: str) -> list[str]:
    if not text:
        return []
    lines = text.split("\n")
    extracted = []
    patterns = [
        re.compile(r"\bremember to\b", re.I),
        re.compile(r"\bremember that\b", re.I),
        re.compile(r"\balways use\b", re.I),
        re.compile(r"\bnever do\b", re.I),
    ]
    for line in lines:
        sentences = re.split(r"(?<=[.!?])\s+", line)
        for s in sentences:
            s_clean = s.strip()
            if any(p.search(s_clean) for p in patterns):
                extracted.append(s_clean)
    return extracted


def _extract_conventions(text: str) -> list[str]:
    if not text:
        return []
    lines = text.split("\n")
    extracted = []
    patterns = [
        re.compile(r"\bconvention:\s*(.*)", re.I),
        re.compile(r"\brule:\s*(.*)", re.I),
    ]
    for line in lines:
        for p in patterns:
            match = p.search(line)
            if match:
                val = match.group(1).strip()
                if val:
                    extracted.append(val)
    return extracted


def _extract_verification_candidates(
    trace_obs: MemoryObservation,
    task_id: str,
    task: Any = None,
) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    if trace_obs.event_type not in ("worker_completed", "worker_failed"):
        return candidates

    expected_cmds: list[str] = []
    if task:
        if task.task_spec and isinstance(task.task_spec, dict):
            cmds = task.task_spec.get("verification_commands") or []
            expected_cmds.extend(cmds)
        if task.constraints and isinstance(task.constraints, dict):
            cmds = task.constraints.get("verification_commands") or []
            expected_cmds.extend(cmds)
    normalized_expected = {c.strip() for c in expected_cmds if isinstance(c, str)}

    metadata = trace_obs.metadata_payload or {}
    commands = metadata.get("commands_run") or []
    ver_cmds = []
    for cmd in commands:
        cmd_str = cmd.get("command")
        exit_code = cmd.get("exit_code")
        if cmd_str and exit_code == 0:
            cmd_stripped = cmd_str.strip()
            is_ver = _is_verification_command(cmd_str) or (cmd_stripped in normalized_expected)
            if is_ver:
                ver_cmds.append(cmd_str)

    verifier_outcome = metadata.get("verifier_outcome")
    if isinstance(verifier_outcome, dict):
        deterministic = verifier_outcome.get("deterministic_verification")
        if isinstance(deterministic, dict) and deterministic.get("status") == "passed":
            passed_commands = deterministic.get("passed_commands") or deterministic.get("commands")
            if isinstance(passed_commands, list):
                for raw_command in passed_commands:
                    if not isinstance(raw_command, str):
                        continue
                    cmd_stripped = raw_command.strip()
                    if not cmd_stripped:
                        continue
                    is_ver = _is_verification_command(cmd_stripped) or (
                        cmd_stripped in normalized_expected
                    )
                    if is_ver:
                        ver_cmds.append(cmd_stripped)

    ver_cmds = list(dict.fromkeys(ver_cmds))

    if ver_cmds:
        evidence_str = f"Verification commands {ver_cmds} executed successfully."
        value_dict = {cmd: cmd for cmd in ver_cmds}
        candidates.append(
            MemoryCandidate(
                category="project",
                memory_key="verification_commands",
                value=value_dict,
                repo_url=trace_obs.repo_url,
                source="worker_result",
                confidence=0.95,
                scope="repo",
                evidence=[evidence_str],
                task_id=task_id,
                session_id=trace_obs.session_id,
                producer="system",
                last_verified_at=trace_obs.observed_at,
                requires_verification=False,
            )
        )
    return candidates


def _extract_pitfall_candidates(
    trace_obs: MemoryObservation, task_id: str
) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    if trace_obs.event_type not in ("worker_completed", "worker_failed"):
        return candidates
    metadata = trace_obs.metadata_payload or {}
    commands = metadata.get("commands_run") or []
    for idx, cmd_fail in enumerate(commands):
        fail_cmd_str = cmd_fail.get("command")
        fail_exit_code = cmd_fail.get("exit_code")
        if fail_cmd_str and fail_exit_code is not None and fail_exit_code != 0:
            for cmd_success in commands[idx + 1 :]:
                success_cmd_str = cmd_success.get("command")
                success_exit_code = cmd_success.get("exit_code")
                if success_cmd_str and success_exit_code == 0:
                    fail_base = _get_base_executable(fail_cmd_str)
                    succ_base = _get_base_executable(success_cmd_str)
                    if fail_base == succ_base and fail_base:
                        evidence_str = (
                            f"Command '{fail_cmd_str}' failed with exit code "
                            f"{fail_exit_code}, resolved by '{success_cmd_str}'"
                        )
                        candidates.append(
                            MemoryCandidate(
                                category="project",
                                memory_key="known_pitfalls",
                                value={
                                    "note": (
                                        f"Command '{fail_cmd_str}' failed; "
                                        f"use '{success_cmd_str}' instead."
                                    ),
                                    "failed_command": fail_cmd_str,
                                    "corrected_command": success_cmd_str,
                                },
                                repo_url=trace_obs.repo_url,
                                source="worker_result",
                                confidence=0.9,
                                scope="repo",
                                evidence=[evidence_str],
                                task_id=task_id,
                                session_id=trace_obs.session_id,
                                producer="system",
                            )
                        )
                        break
    return candidates


def _extract_remember_candidates(
    trace_obs: MemoryObservation, task_id: str
) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    if trace_obs.event_type != "interaction_resolved":
        return candidates
    texts_to_scan = []
    if trace_obs.summary:
        texts_to_scan.append(trace_obs.summary)
    if trace_obs.content:
        texts_to_scan.append(trace_obs.content)

    for text in texts_to_scan:
        remember_sentences = _extract_remember_sentences(text)
        for sentence in remember_sentences:
            candidates.append(
                MemoryCandidate(
                    category="project" if trace_obs.repo_url else "personal",
                    memory_key="remembered_instruction",
                    value={"instruction": sentence},
                    repo_url=trace_obs.repo_url,
                    source="operator" if trace_obs.source == "operator" else "system",
                    confidence=0.75,
                    scope="repo" if trace_obs.repo_url else "global",
                    evidence=[f"Extracted from text: '{sentence}'"],
                    task_id=task_id,
                    session_id=trace_obs.session_id,
                    producer="system",
                )
            )
    return candidates


def _extract_convention_candidates(
    trace_obs: MemoryObservation, task_id: str
) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    if trace_obs.event_type not in ("worker_completed", "worker_failed"):
        return candidates
    texts_to_scan = []
    if trace_obs.summary:
        texts_to_scan.append(trace_obs.summary)
    if trace_obs.content:
        texts_to_scan.append(trace_obs.content)

    for text in texts_to_scan:
        conventions = _extract_conventions(text)
        for conv in conventions:
            candidates.append(
                MemoryCandidate(
                    category="project",
                    memory_key="repo_convention",
                    value={"convention": conv},
                    repo_url=trace_obs.repo_url,
                    source="worker_result",
                    confidence=0.9,
                    scope="repo",
                    evidence=[f"Convention/rule keyword matched in trace: '{conv}'"],
                    task_id=task_id,
                    session_id=trace_obs.session_id,
                    producer="system",
                )
            )
    return candidates


def _save_extracted_candidates(
    session: Session,
    task_id: str,
    trace_obs: MemoryObservation,
    candidates: list[MemoryCandidate],
    obs_repo: ObservationRepository,
) -> None:
    for cand in candidates:
        cand_dict = cand.model_dump(mode="json")
        cand_dict, _ = redact_private_tags_recursive(cand_dict)

        obs_repo.create(
            task_id=task_id,
            session_id=trace_obs.session_id,
            repo_url=trace_obs.repo_url,
            source="system",
            event_type="extracted_candidate",
            summary=f"Extracted candidate: {cand.memory_key}",
            content=(f"Key: {cand.memory_key}\nSource trace observation ID: {trace_obs.id}"),
            metadata_payload={
                "memory_candidate": cand_dict,
                "parent_observation_id": trace_obs.id,
            },
            admission_status="pending",
        )


def _extract_candidates_from_task_text(
    session: Session,
    task_id: str,
    task: Any,
    obs_repo: ObservationRepository,
) -> None:
    if not (task and task.task_text):
        return
    check_stmt = select(MemoryObservation).where(
        MemoryObservation.task_id == task_id,
        MemoryObservation.event_type == "extracted_candidate",
    )
    existing_children = list(session.scalars(check_stmt))
    task_text_already_extracted = any(
        (child.metadata_payload or {}).get("parent_observation_id") == f"task-{task_id}"
        for child in existing_children
    )
    if not task_text_already_extracted:
        remember_sentences = _extract_remember_sentences(task.task_text)
        for sentence in remember_sentences:
            cand = MemoryCandidate(
                category="project" if task.repo_url else "personal",
                memory_key="remembered_instruction",
                value={"instruction": sentence},
                repo_url=task.repo_url,
                source="operator",
                confidence=0.75,
                scope="repo" if task.repo_url else "global",
                evidence=[f"Extracted from task instruction: '{sentence}'"],
                task_id=task_id,
                session_id=task.session_id,
                producer="system",
            )
            cand_dict = cand.model_dump(mode="json")
            cand_dict, _ = redact_private_tags_recursive(cand_dict)

            obs_repo.create(
                task_id=task_id,
                session_id=task.session_id,
                repo_url=task.repo_url,
                source="system",
                event_type="extracted_candidate",
                summary=f"Extracted candidate: {cand.memory_key}",
                content=f"Key: {cand.memory_key}\nSource: task description",
                metadata_payload={
                    "memory_candidate": cand_dict,
                    "parent_observation_id": f"task-{task_id}",
                },
                admission_status="pending",
            )


def extract_candidates_from_task_traces(session: Session, task_id: str) -> None:
    """Scan task observations and finalization text to extract deterministic memory candidates."""
    obs_repo = ObservationRepository(session)
    statement = select(MemoryObservation).where(
        MemoryObservation.task_id == task_id,
        MemoryObservation.source.in_(["worker", "operator", "orchestrator"]),
        MemoryObservation.event_type.in_(
            ["worker_completed", "worker_failed", "interaction_resolved", "task_finalized"]
        ),
    )
    trace_obs_list = list(session.scalars(statement))

    task_statement = select(Task).where(Task.id == task_id)
    task = session.scalar(task_statement)

    check_stmt = select(MemoryObservation).where(
        MemoryObservation.task_id == task_id,
        MemoryObservation.event_type == "extracted_candidate",
    )
    existing_children = list(session.scalars(check_stmt))
    extracted_parent_ids = {
        (child.metadata_payload or {}).get("parent_observation_id") for child in existing_children
    }

    for trace_obs in trace_obs_list:
        if trace_obs.id in extracted_parent_ids:
            continue

        candidates: list[MemoryCandidate] = []
        candidates.extend(_extract_verification_candidates(trace_obs, task_id, task))
        candidates.extend(_extract_pitfall_candidates(trace_obs, task_id))
        candidates.extend(_extract_remember_candidates(trace_obs, task_id))
        candidates.extend(_extract_convention_candidates(trace_obs, task_id))

        _save_extracted_candidates(session, task_id, trace_obs, candidates, obs_repo)

    _extract_candidates_from_task_text(session, task_id, task, obs_repo)


class ObservationMemoryBridge:
    """Synchronous, local bridge to promote pending observations to memory candidates."""

    @staticmethod
    def bridge_observations(session: Session, task_id: str) -> dict[str, Any]:
        """Fetch pending observations for a task, validate candidate payloads, and run admission."""
        # First perform trace-to-candidate extraction to populate child pending observations
        try:
            extract_candidates_from_task_traces(session, task_id)
            session.flush()
        except Exception as extract_exc:
            logger.error(
                "Failed to run deterministic trace-to-candidate extraction: %s",
                extract_exc,
                exc_info=True,
            )

        obs_repo = ObservationRepository(session)
        statement = select(MemoryObservation).where(
            MemoryObservation.task_id == task_id,
            MemoryObservation.admission_status == "pending",
        )
        pending_obs = list(session.scalars(statement))
        if not pending_obs:
            return _bridge_summary(session, task_id)

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
        return _bridge_summary(session, task_id)
