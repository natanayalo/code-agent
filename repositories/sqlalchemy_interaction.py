"""Human-interaction and inbound-delivery SQLAlchemy repositories."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from db.base import utc_now
from db.enums import HumanInteractionHitlMode, HumanInteractionStatus, HumanInteractionType
from db.models import HumanInteraction, InboundDelivery


class HumanInteractionRepository:
    """Persist and query human interaction checkpoints."""

    _TASK_SPEC_SOURCE = "task_spec"
    _TASK_SPEC_INTERACTION_TYPES = (
        HumanInteractionType.CLARIFICATION,
        HumanInteractionType.PERMISSION,
    )

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_by_task(
        self,
        *,
        task_id: str,
        interaction_types: tuple[HumanInteractionType, ...] | None = None,
        statuses: tuple[HumanInteractionStatus, ...] | None = None,
    ) -> list[HumanInteraction]:
        statement = (
            select(HumanInteraction)
            .where(HumanInteraction.task_id == task_id)
            .order_by(HumanInteraction.created_at.asc())
        )
        if interaction_types is not None:
            statement = statement.where(HumanInteraction.interaction_type.in_(interaction_types))
        if statuses is not None:
            statement = statement.where(HumanInteraction.status.in_(statuses))
        return list(self.session.scalars(statement))

    def list_pending_with_task_context(self) -> list[tuple[HumanInteraction, Any]]:
        from db.models import Task

        statement = (
            select(HumanInteraction, Task)
            .join(Task, Task.id == HumanInteraction.task_id)
            .where(HumanInteraction.status == HumanInteractionStatus.PENDING)
            .order_by(HumanInteraction.created_at.desc())
        )
        # Type ignored because Task is a complex model imported dynamically
        rows = self.session.execute(statement).all()
        return [(row[0], row[1]) for row in rows]

    def record_response(
        self,
        interaction_id: str,
        *,
        task_id: str,
        response_data: Mapping[str, Any],
        status: HumanInteractionStatus = HumanInteractionStatus.RESOLVED,
    ) -> tuple[HumanInteraction | None, bool]:
        interaction = self.session.get(HumanInteraction, interaction_id)
        if interaction is None or interaction.task_id != task_id:
            return None, False
        if interaction.status != HumanInteractionStatus.PENDING:
            return interaction, False

        interaction.status = status
        interaction.response_data = dict(response_data)
        interaction.updated_at = utc_now()
        self.session.flush()
        return interaction, True

    def sync_task_spec_flags(
        self, *, task_id: str, task_spec: dict[str, Any]
    ) -> list[HumanInteraction]:
        desired = self._extract_desired_interactions(task_id, task_spec)

        existing = self.list_by_task(
            task_id=task_id,
            interaction_types=self._TASK_SPEC_INTERACTION_TYPES,
        )
        task_spec_rows = [
            row
            for row in existing
            if isinstance(row.data, Mapping) and row.data.get("source") == self._TASK_SPEC_SOURCE
        ]

        for interaction_type in self._TASK_SPEC_INTERACTION_TYPES:
            self._sync_interaction_type(
                task_id, interaction_type, desired.get(interaction_type), task_spec_rows
            )

        self.session.flush()
        return self.list_by_task(
            task_id=task_id,
            interaction_types=self._TASK_SPEC_INTERACTION_TYPES,
        )

    def _extract_desired_interactions(
        self, task_id: str, task_spec: dict[str, Any]
    ) -> dict[HumanInteractionType, tuple[str, dict[str, Any]]]:
        desired: dict[HumanInteractionType, tuple[str, dict[str, Any]]] = {}

        if bool(task_spec.get("requires_clarification")):
            raw_questions = task_spec.get("clarification_questions")
            clarification_questions = raw_questions if isinstance(raw_questions, list) else []
            goal_text_raw = task_spec.get("goal")
            goal_text = goal_text_raw.strip() if isinstance(goal_text_raw, str) else ""
            questions = [
                question.strip()
                for question in clarification_questions
                if isinstance(question, str) and question.strip()
            ]
            if not questions:
                if goal_text:
                    questions = [
                        "What exact repo, files, behavior, or failure should the worker target "
                        f"for: {goal_text}?"
                    ]
                else:
                    questions = [
                        "What exact repo, files, behavior, or failure should the worker target?"
                    ]
            desired[HumanInteractionType.CLARIFICATION] = (
                "Task requires clarification before execution can continue.",
                {
                    "source": self._TASK_SPEC_SOURCE,
                    "resume_token": f"clarification-{task_id}",
                    "questions": questions,
                },
            )

        if bool(task_spec.get("requires_permission")):
            reason_raw = task_spec.get("permission_reason")
            reason = (
                reason_raw.strip()
                if isinstance(reason_raw, str) and reason_raw.strip()
                else "Task requires explicit permission before execution can continue."
            )
            desired[HumanInteractionType.PERMISSION] = (
                reason,
                {
                    "source": self._TASK_SPEC_SOURCE,
                    "resume_token": f"permission-{task_id}",
                    "reason": reason,
                    "risk_level": task_spec.get("risk_level"),
                },
            )
        return desired

    def _compute_decision_key(
        self, interaction_type: HumanInteractionType, summary: str, data: dict[str, Any]
    ) -> str:
        """Compute a stable decision key ignoring volatile fields."""
        stable_data = {
            k: v
            for k, v in data.items()
            if k not in {"source", "resume_token", "created_at", "updated_at"}
        }
        payload = {
            "type": str(interaction_type),
            "summary": summary,
            "data": stable_data,
        }
        content = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha256(content).hexdigest()

    def _has_resolved_equivalent(
        self,
        decision_key: str,
        desired_resume_token: str | None,
        resolved_rows: list[HumanInteraction],
    ) -> bool:
        if any(row.decision_key == decision_key for row in resolved_rows):
            return True
        if desired_resume_token and desired_resume_token.strip():
            if any(
                isinstance(row.data, Mapping)
                and row.data.get("resume_token") == desired_resume_token
                for row in resolved_rows
            ):
                return True
        return False

    def _sync_interaction_type(
        self,
        task_id: str,
        interaction_type: HumanInteractionType,
        desired_payload: tuple[str, dict[str, Any]] | None,
        task_spec_rows: list[HumanInteraction],
    ) -> None:
        interaction_rows = [
            row for row in task_spec_rows if row.interaction_type == interaction_type
        ]
        pending_rows = [
            row for row in interaction_rows if row.status == HumanInteractionStatus.PENDING
        ]
        resolved_rows = [
            row for row in interaction_rows if row.status == HumanInteractionStatus.RESOLVED
        ]
        active_rows = [
            row for row in interaction_rows if row.status != HumanInteractionStatus.CANCELLED
        ]

        if desired_payload is None:
            for row in pending_rows:
                row.status = HumanInteractionStatus.CANCELLED
            return

        summary, data = desired_payload
        decision_key = self._compute_decision_key(interaction_type, summary, data)
        desired_resume_token = data.get("resume_token") if isinstance(data, Mapping) else None

        if self._has_resolved_equivalent(decision_key, desired_resume_token, resolved_rows):
            for duplicate in pending_rows:
                duplicate.status = HumanInteractionStatus.CANCELLED
            return

        if pending_rows:
            primary = pending_rows[0]
            primary.summary = summary
            primary.data = data
            primary.decision_key = decision_key
            primary.hitl_mode = HumanInteractionHitlMode.REQUIRE_APPROVAL
            primary.response_data = None
            primary.status = HumanInteractionStatus.PENDING
            for duplicate in pending_rows[1:]:
                duplicate.status = HumanInteractionStatus.CANCELLED
            return

        if active_rows:
            primary = active_rows[-1]
            if primary.status == HumanInteractionStatus.PENDING:
                primary.summary = summary
                primary.data = data
                primary.decision_key = decision_key
                primary.hitl_mode = HumanInteractionHitlMode.REQUIRE_APPROVAL
                primary.response_data = None
                return
            if primary.summary != summary or primary.data != data:
                self.session.add(
                    HumanInteraction(
                        task_id=task_id,
                        interaction_type=interaction_type,
                        status=HumanInteractionStatus.PENDING,
                        summary=summary,
                        decision_key=decision_key,
                        hitl_mode=HumanInteractionHitlMode.REQUIRE_APPROVAL,
                        data=data,
                    )
                )
            return

        self.session.add(
            HumanInteraction(
                task_id=task_id,
                interaction_type=interaction_type,
                status=HumanInteractionStatus.PENDING,
                summary=summary,
                decision_key=decision_key,
                hitl_mode=HumanInteractionHitlMode.REQUIRE_APPROVAL,
                data=data,
            )
        )


class InboundDeliveryRepository:
    """Persist and query webhook delivery dedupe claims."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        channel: str,
        delivery_id: str,
        task_id: str | None = None,
    ) -> InboundDelivery:
        delivery = InboundDelivery(
            channel=channel,
            delivery_id=delivery_id,
            task_id=task_id,
        )
        self.session.add(delivery)
        self.session.flush()
        return delivery

    def get_by_channel_delivery(
        self,
        *,
        channel: str,
        delivery_id: str,
    ) -> InboundDelivery | None:
        statement = select(InboundDelivery).where(
            InboundDelivery.channel == channel,
            InboundDelivery.delivery_id == delivery_id,
        )
        return self.session.scalar(statement)

    def attach_task_if_unassigned(
        self,
        *,
        channel: str,
        delivery_id: str,
        task_id: str,
    ) -> InboundDelivery | None:
        statement = (
            update(InboundDelivery)
            .where(
                InboundDelivery.channel == channel,
                InboundDelivery.delivery_id == delivery_id,
                InboundDelivery.task_id.is_(None),
            )
            .values(task_id=task_id)
            .returning(InboundDelivery.id)
        )
        result = self.session.execute(statement)
        updated_id = result.scalar_one_or_none()
        if updated_id is None:
            return None
        self.session.flush()
        return self.get_by_channel_delivery(channel=channel, delivery_id=delivery_id)
