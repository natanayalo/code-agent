"""Telegram webhook adapter — maps Telegram Update objects to task submissions."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_task_service
from orchestrator.execution import (
    SubmissionSession,
    TaskExecutionService,
    TaskSubmission,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])

# ---------------------------------------------------------------------------
# Telegram Update shape (minimal subset we care about)
# ---------------------------------------------------------------------------
# Telegram sends the full Update object but we only parse what we need.
# Extra fields are ignored (extra="ignore") because Telegram adds new fields
# without notice and unknown fields must never break the endpoint.


class TelegramUser(BaseModel):
    """Partial Telegram User object."""

    model_config = ConfigDict(extra="ignore")

    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class TelegramChat(BaseModel):
    """Partial Telegram Chat object."""

    model_config = ConfigDict(extra="ignore")

    id: int


class TelegramMessage(BaseModel):
    """Partial Telegram Message object."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message_id: int
    chat: TelegramChat
    from_: TelegramUser | None = Field(default=None, alias="from")
    text: str | None = None
    caption: str | None = None


class TelegramUpdate(BaseModel):
    """Partial Telegram Update object.

    Only ``message`` and ``channel_post`` are handled in this slice.  Edited
    messages, callback queries, etc. are silently acknowledged (200 OK) without
    creating a task.
    """

    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: TelegramMessage | None = None
    channel_post: TelegramMessage | None = None


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class TelegramWebhookResponse(BaseModel):
    """Response returned to Telegram after processing an Update."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    task_id: str | None = None
    session_id: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CHANNEL = "telegram"
_MAX_TEXT_LENGTH = 10_000


def _build_display_name(user: TelegramUser | None) -> str | None:
    """Return a human-readable name from a Telegram user object, or None."""
    if user is None:
        return None
    parts = [p for p in [user.first_name, user.last_name] if p]
    return " ".join(parts) if parts else user.username


def _to_task_submission(msg: TelegramMessage, text: str) -> TaskSubmission:
    """Convert a Telegram message and its text into a TaskSubmission."""
    # Use the Telegram chat id as the thread identifier so all messages in
    # the same conversation hit the same Session.
    external_thread_id = f"telegram:chat:{msg.chat.id}"

    # Use the Telegram user id as the user identifier, namespaced under
    # "telegram:" to isolate from other adapters.
    if msg.from_ is not None:
        external_user_id = f"telegram:user:{msg.from_.id}"
    else:
        # Channel posts have no sender; use a per-chat anonymous sentinel.
        external_user_id = f"telegram:chat:{msg.chat.id}:anonymous"

    session = SubmissionSession(
        channel=_CHANNEL,
        external_user_id=external_user_id,
        external_thread_id=external_thread_id,
        display_name=_build_display_name(msg.from_),
    )
    return TaskSubmission(
        task_text=text,
        session=session,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/webhook",
    response_model=TelegramWebhookResponse,
    status_code=status.HTTP_200_OK,
)
def receive_telegram_update(
    update: TelegramUpdate,
    background_tasks: BackgroundTasks,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TelegramWebhookResponse:
    """Accept a Telegram Update and enqueue the message text as a task.

    Telegram requires a 200 response for *all* delivered updates — including
    those we choose not to act on — otherwise it will retry delivery.  We
    therefore return 200 even for non-message updates (edited messages, polls,
    etc.) or messages with no text content.
    """
    msg = update.message or update.channel_post

    if msg is None:
        logger.debug("telegram update_id=%d has no message, ignoring", update.update_id)
        return TelegramWebhookResponse(ok=True, detail="no_message")

    text = (msg.text or msg.caption or "").strip()
    if not text:
        logger.debug(
            "telegram update_id=%d message_id=%d has no text, ignoring",
            update.update_id,
            msg.message_id,
        )
        return TelegramWebhookResponse(ok=True, detail="no_text")

    if len(text) > _MAX_TEXT_LENGTH:
        logger.warning(
            "telegram update_id=%d message_id=%d text exceeds 10 000 chars, ignoring",
            update.update_id,
            msg.message_id,
        )
        return TelegramWebhookResponse(ok=True, detail="text_too_long")

    submission = _to_task_submission(msg, text)
    task_snapshot, persisted = task_service.create_task(submission)
    background_tasks.add_task(task_service.submit_task, submission, persisted)

    logger.info(
        "telegram update_id=%d enqueued task_id=%s",
        update.update_id,
        task_snapshot.task_id,
    )
    return TelegramWebhookResponse(
        ok=True,
        task_id=task_snapshot.task_id,
        session_id=task_snapshot.session_id,
    )
