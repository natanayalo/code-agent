"""Structured review prompt construction helpers."""

from __future__ import annotations

import json
from pathlib import Path

from workers.adapter_utils import truncate_detail_keep_tail
from workers.markdown import markdown_fence_for_content
from workers.prompt import (
    _fenced_text_block_lines,
    _fenced_text_block_overhead,
    build_build_test_section,
)
from workers.prompt_workspace import _read_text_prefix, read_workspace_repo_guidance

_SECTION_SEPARATOR_OVERHEAD_BUFFER = 32
_GUIDANCE_OVERHEAD_BUFFER = 100
_REVIEW_ROLE_SECTION = "\n".join(
    [
        "## Review Role",
        "You are the review worker for code-agent.",
        "Focus on high-confidence, actionable findings grounded in the workspace state.",
        "You have full tool access; use `git diff`, `read_file`, and other tools to inspect ",
        "the changes and their impact before finalizing your review.",
        "Prefer precision over recall and skip style-only or speculative comments.",
        "Do not propose broad rewrites when a focused finding is sufficient.",
    ]
)
_REVIEW_SCHEMA_PAYLOAD = {
    "reviewer_kind": "string",
    "summary": "string",
    "confidence": 0.0,
    "outcome": "no_findings|findings",
    "findings": [
        {
            "severity": "low|medium|high|critical",
            "category": "string",
            "confidence": 0.0,
            "file_path": "string",
            "line_start": 1,
            "line_end": 1,
            "title": "string",
            "why_it_matters": "string",
            "evidence": "string|null",
            "suggested_fix": "string|null",
        }
    ],
}
_REVIEW_OUTPUT_CONTRACT_TEMPLATE = "\n".join(
    [
        "## Output Contract",
        "Return exactly one JSON object. Your response MUST NOT contain any markdown ",
        "fences or extra prose outside of the JSON payload.",
        "Schema:",
        "```json",
        "{schema_json}",
        "```",
        "Rules:",
        "- Use outcome `no_findings` with an empty `findings` list when nothing actionable exists.",
        "- Use outcome `findings` only when at least one concrete actionable finding exists.",
        "- Base your findings on the actual file contents and diff observed via tools.",
    ]
)


def build_review_prompt(
    *,
    workspace_path: Path,
    review_context_packet: str,
    reviewer_kind: str = "worker_self_review",
    task_text: str | None = None,
) -> str:
    """Assemble a review-only prompt separated from execution/tool-loop prompts."""
    # Reserve a small budget buffer for \n\n separators between prompt sections
    # and a buffer for block labels/fences added after reading guidance.
    total_guidance_budget = (
        DEFAULT_REVIEW_GUIDANCE_MAX_CHARACTERS
        - _SECTION_SEPARATOR_OVERHEAD_BUFFER
        - _GUIDANCE_OVERHEAD_BUFFER
    )
    agents_guidance, agents_assets_guidance = read_workspace_repo_guidance(
        workspace_path,
        max_characters=total_guidance_budget,
    )

    guidance_lines: list[str] = []
    consumed_guidance_characters = 0
    guidance_block_count = 0

    if agents_guidance is not None:
        if guidance_block_count == 0:
            consumed_guidance_characters += len("## Review Guidance") + 1
        else:
            consumed_guidance_characters += 1  # separator between blocks

        fence = markdown_fence_for_content(agents_guidance)
        guidance_lines.extend(
            _fenced_text_block_lines("AGENTS.md guidance:", agents_guidance, fence=fence)
        )
        consumed_guidance_characters += len(agents_guidance) + _fenced_text_block_overhead(
            "AGENTS.md guidance:", agents_guidance, fence=fence
        )
        guidance_block_count += 1

    if agents_assets_guidance is not None:
        if guidance_block_count == 0:
            consumed_guidance_characters += len("## Review Guidance") + 1
        else:
            consumed_guidance_characters += 1  # separator between blocks

        fence = markdown_fence_for_content(agents_assets_guidance)
        guidance_lines.extend(
            _fenced_text_block_lines(".agents guidance:", agents_assets_guidance, fence=fence)
        )
        consumed_guidance_characters += len(agents_assets_guidance) + _fenced_text_block_overhead(
            ".agents guidance:", agents_assets_guidance, fence=fence
        )
        guidance_block_count += 1

    if guidance_lines:
        # Account for "## Review Guidance" header and the newline after it
        consumed_guidance_characters += len("## Review Guidance") + 1
        if guidance_block_count > 1:
            # Account for newlines between multiple blocks joined by "\n"
            consumed_guidance_characters += guidance_block_count - 1

    review_guidance = read_workspace_review_guidance(
        workspace_path,
        max_characters=max(total_guidance_budget - consumed_guidance_characters, 0),
    )
    if review_guidance is not None:
        if guidance_block_count == 0:
            consumed_guidance_characters += len("## Review Guidance") + 1
        else:
            consumed_guidance_characters += 1  # separator between blocks

        fence = markdown_fence_for_content(review_guidance)
        guidance_lines.extend(
            _fenced_text_block_lines("REVIEW.md guidance:", review_guidance, fence=fence)
        )
        consumed_guidance_characters += len(review_guidance) + _fenced_text_block_overhead(
            "REVIEW.md guidance:", review_guidance, fence=fence
        )
        guidance_block_count += 1

    build_test_context = build_build_test_section(workspace_path)
    guidance_section = ""
    if guidance_lines:
        guidance_section = "\n".join(["## Review Guidance", *guidance_lines])

    task_lines = [
        "## Review Task",
        f"Reviewer kind: {reviewer_kind}",
    ]
    if task_text:
        task_lines.append(f"Task objective: {task_text}")
    task_lines.extend(
        [
            "Evaluate:",
            "1. Does the delivered diff satisfy the task objective?",
            "2. Are there unintended behavioral changes?",
            "3. Are there obvious logical issues?",
            "4. Are relevant tests or checks missing for changed behavior?",
        ]
    )

    schema_payload = {**_REVIEW_SCHEMA_PAYLOAD, "reviewer_kind": reviewer_kind}
    schema_json = json.dumps(schema_payload, indent=2)
    output_section = _REVIEW_OUTPUT_CONTRACT_TEMPLATE.format(schema_json=schema_json)

    sections = [
        _REVIEW_ROLE_SECTION,
        guidance_section,
        build_test_context or "",
        "\n".join(task_lines),
        f"## Review Context Packet\n{review_context_packet}"
        if review_context_packet.strip()
        else "",
        output_section,
    ]
    return "\n\n".join(section for section in sections if section.strip())


DEFAULT_REVIEW_GUIDANCE_MAX_CHARACTERS = 3000


def read_workspace_review_guidance(
    workspace_path: Path,
    *,
    max_characters: int = DEFAULT_REVIEW_GUIDANCE_MAX_CHARACTERS,
) -> str | None:
    """Return bounded REVIEW.md guidance from the workspace root when present."""
    review_path = workspace_path / "REVIEW.md"
    if not review_path.is_file() or max_characters <= 0:
        return None
    try:
        contents = _read_text_prefix(review_path, max_characters=max_characters + 1).strip()
    except OSError:
        return None
    if not contents:
        return None
    return truncate_detail_keep_tail(contents, max_characters=max_characters)
