"""Unit tests for delivery of durable memory to native worker prompts."""

from __future__ import annotations

from workers.base import WorkerRequest
from workers.prompt import build_effective_system_prompt, build_system_prompt
from workers.prompt_memory import native_memory_delivery_receipt


def _accepted_memory_request(
    *, key: str = "accepted-command", value: str = "printf marker"
) -> WorkerRequest:
    return WorkerRequest(
        task_text="Use the memory",
        memory_context={
            "project": [
                {
                    "memory_key": key,
                    "value": {"command": value},
                    "gate_status": "accepted",
                }
            ]
        },
    )


def test_native_memory_delivery_receipt_does_not_expose_values(tmp_path) -> None:
    request = _accepted_memory_request(key="safe-key", value="secret-looking-command")
    system_prompt = build_system_prompt(request, tmp_path)
    native_prompt = f"{system_prompt}\n\n## Native Execution Task\n{request.task_text}"

    receipt = native_memory_delivery_receipt(
        request, system_prompt=system_prompt, native_prompt=native_prompt
    )

    assert receipt == {
        "accepted_memory_keys": ["safe-key"],
        "delivered_memory_keys": ["safe-key"],
        "missing_accepted_memory_keys": [],
        "complete": True,
    }
    assert "secret-looking-command" not in str(receipt)


def test_native_memory_delivery_receipt_handles_private_tag_redaction(tmp_path) -> None:
    request = _accepted_memory_request(value="printf <private>secret-marker</private>")
    system_prompt = build_effective_system_prompt(request, tmp_path)

    receipt = native_memory_delivery_receipt(
        request, system_prompt=system_prompt, native_prompt=system_prompt
    )

    assert receipt["complete"] is True
    assert receipt["delivered_memory_keys"] == ["accepted-command"]


def test_effective_system_prompt_appends_memory_to_role_override(tmp_path) -> None:
    prompt = build_effective_system_prompt(
        _accepted_memory_request(),
        tmp_path,
        system_prompt_override="## Specialist Role\nPerform independent verification.",
    )

    assert "## Specialist Role" in prompt
    assert "## Durable Memories" in prompt
    assert "accepted-command" in prompt


def test_effective_system_prompt_preserves_canonical_memory_section(tmp_path) -> None:
    request = _accepted_memory_request()
    prompt = build_effective_system_prompt(request, tmp_path)
    receipt = native_memory_delivery_receipt(request, system_prompt=prompt, native_prompt=prompt)

    assert receipt["complete"] is True


def test_effective_system_prompt_retains_key_for_oversized_accepted_memory(tmp_path) -> None:
    request = _accepted_memory_request(value="x" * 4_000)
    prompt = build_effective_system_prompt(request, tmp_path)
    receipt = native_memory_delivery_receipt(request, system_prompt=prompt, native_prompt=prompt)

    assert "**accepted-command** [value omitted by prompt budget]" in prompt
    assert receipt["complete"] is True


def test_accepted_project_memory_precedes_oversized_profile_entries(tmp_path) -> None:
    request = WorkerRequest(
        task_text="Use the requested memory",
        memory_context={
            "project": [
                {
                    "memory_key": "m28-useful-hit-codex",
                    "value": {"command": "printf m28-useful_hit-marker"},
                    "gate_status": "accepted",
                    "advisory_strength": 0.95,
                },
                {
                    "memory_key": "verification_commands",
                    "value": {"command": "x" * 4_000},
                    "gate_status": "accepted",
                    "advisory_strength": 1.0,
                },
            ],
            "repository_profile": {
                "verification_commands": [
                    {
                        "memory_key": "verification_commands",
                        "value": {"command": "x" * 4_000},
                        "gate_status": "accepted",
                        "advisory_strength": 1.0,
                    }
                ],
                "general_facts": [
                    {
                        "memory_key": "m28-useful-hit-codex",
                        "value": {"command": "printf m28-useful_hit-marker"},
                        "gate_status": "accepted",
                        "advisory_strength": 0.95,
                    }
                ],
            },
        },
    )

    prompt = build_effective_system_prompt(request, tmp_path)
    receipt = native_memory_delivery_receipt(request, system_prompt=prompt, native_prompt=prompt)

    assert "**verification_commands** [value omitted by prompt budget]" in prompt
    assert "printf m28-useful_hit-marker" in prompt
    assert receipt["delivered_memory_keys"] == [
        "m28-useful-hit-codex",
        "verification_commands",
    ]
