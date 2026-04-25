"""Shared role-native message shaping helpers for runtime adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from workers.cli_runtime import CliRuntimeMessage


@dataclass(frozen=True, slots=True)
class RoleNativeMessage:
    """Provider-agnostic role-native message shape."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_name: str | None = None


def build_role_native_transcript(
    messages: tuple[CliRuntimeMessage, ...],
) -> list[RoleNativeMessage]:
    """Map shared runtime transcript messages into role-native transcript entries."""
    role_native: list[RoleNativeMessage] = []
    for message in messages:
        if message.role == "tool":
            role_native.append(
                RoleNativeMessage(
                    role="tool",
                    content=message.content,
                    tool_name=message.tool_name,
                )
            )
            continue
        role_native.append(
            RoleNativeMessage(
                role=message.role,
                content=message.content,
                tool_name=None,
            )
        )
    return role_native


def serialize_openai_compatible_messages(
    messages: list[RoleNativeMessage],
) -> list[dict[str, str]]:
    """Serialize role-native transcript entries for OpenAI-compatible adapters.

    OpenAI-compatible chat endpoints generally require `tool_call_id` for `tool`
    role messages. Runtime transcripts in this worker do not carry call IDs, so
    we keep the `tool` role in shared strategy and serialize tool observations as
    user messages tagged with tool metadata.
    """
    serialized: list[dict[str, str]] = []
    for message in messages:
        if message.role == "tool":
            tool_label = message.tool_name or "unknown_tool"
            serialized.append(
                {
                    "role": "user",
                    "content": f"Tool result ({tool_label}):\n{message.content}",
                }
            )
            continue
        serialized.append(
            {
                "role": message.role,
                "content": message.content,
            }
        )
    return serialized
