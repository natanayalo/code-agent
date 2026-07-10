"""Unit tests for worker system prompt construction."""

from __future__ import annotations

from datetime import UTC, datetime

from orchestrator.state import MemoryEntry, ObservationContextEntry, RepositoryMemoryProfile
from workers.base import WorkerRequest
from workers.prompt import build_system_prompt


def test_build_system_prompt_respects_delivery_mode_summary(tmp_path) -> None:
    """System prompt should use analysis-focused role for summary delivery mode."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Analyze the codebase",
        repo_url="https://example.com/repo.git",
        task_spec={"delivery_mode": "summary"},
        constraints={"read_only": True},
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "coding execution worker" in prompt
    assert "read permissions" in prompt
    assert "analyze files" in prompt
    assert "Do not modify files." in prompt
    assert "Prefer minimal edits" not in prompt


def test_build_system_prompt_respects_delivery_mode_workspace(tmp_path) -> None:
    """System prompt should use analysis-focused role."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Fix the bug",
        repo_url="https://example.com/repo.git",
        task_spec={"delivery_mode": "workspace"},
        constraints={"read_only": False},
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "coding execution worker" in prompt
    assert "read/write permissions" in prompt
    assert "make smallest safe changes" in prompt
    assert "Do not modify any files" not in prompt
    assert "Prefer minimal edits" in prompt


def test_build_system_prompt_omits_verbose_json_bloats(tmp_path) -> None:
    """System prompt should extract key fields instead of dumping raw JSON blobs."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Implement feature",
        repo_url="https://example.com/repo.git",
        task_spec={
            "goal": "Implement feature",
            "assumptions": ["Assume X"],
            "acceptance_criteria": ["Done when Y"],
            "delivery_mode": "workspace",
        },
        memory_context={"large": "context"},
        task_plan={"steps": ["step1"]},
        constraints={"risk_level": "low", "some_internal_junk": "junk"},
    )

    prompt = build_system_prompt(request, tmp_path)

    # Key fields should be present as markdown
    assert "Assumptions:" in prompt
    assert "- Assume X" in prompt
    assert "Acceptance criteria:" in prompt
    assert "- Done when Y" in prompt

    # Large/internal JSON blobs should be omitted
    assert '"large": "context"' not in prompt
    assert '"steps": ["step1"]' not in prompt
    assert '"some_internal_junk": "junk"' not in prompt

    # Filtered constraints should be present
    assert '"risk_level": "low"' in prompt


def test_build_system_prompt_redacts_private_tagged_sections(tmp_path) -> None:
    """Worker prompts should never include user-marked private blocks."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Fix <private>secret task detail</private>",
        repo_url="https://example.com/repo.git",
        task_spec={
            "acceptance_criteria": ["Do not reveal <private>secret criterion</private>"],
        },
        memory_context={
            "personal": [
                {
                    "memory_key": "style",
                    "value": {"note": "<private>secret memory</private>"},
                }
            ],
            "observations": [
                {
                    "id": "obs-1",
                    "observed_at": "2026-07-04T12:00:00Z",
                    "source": "worker",
                    "event_type": "worker_completed",
                    "summary": "Saw <private>secret observation</private>",
                }
            ],
        },
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "secret task detail" not in prompt
    assert "secret criterion" not in prompt
    assert "secret memory" not in prompt
    assert "secret observation" not in prompt
    assert prompt.count("[redacted-private]") == 4


def test_build_system_prompt_filters_tools_by_permission(tmp_path) -> None:
    """System prompt should only list tools permitted by the current constraints."""
    from tools import DEFAULT_TOOL_REGISTRY, EXECUTE_BASH_TOOL_NAME, VIEW_FILE_TOOL_NAME

    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")

    # Read-only request
    request_ro = WorkerRequest(
        task_text="Check status",
        repo_url="https://example.com/repo.git",
        constraints={"read_only": True},
    )
    prompt_ro = build_system_prompt(request_ro, tmp_path, tool_registry=DEFAULT_TOOL_REGISTRY)

    assert f"### `{VIEW_FILE_TOOL_NAME}`" in prompt_ro
    assert f"### `{EXECUTE_BASH_TOOL_NAME}`" not in prompt_ro

    # Write request
    request_rw = WorkerRequest(
        task_text="Fix bug",
        repo_url="https://example.com/repo.git",
        constraints={"read_only": False},
    )
    prompt_rw = build_system_prompt(request_rw, tmp_path, tool_registry=DEFAULT_TOOL_REGISTRY)

    assert f"### `{VIEW_FILE_TOOL_NAME}`" in prompt_rw
    assert f"### `{EXECUTE_BASH_TOOL_NAME}`" in prompt_rw


def test_build_system_prompt_honors_explicit_requested_tool_subset(tmp_path) -> None:
    from tools import DEFAULT_TOOL_REGISTRY, VIEW_FILE_TOOL_NAME

    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Inspect files only",
        repo_url="https://example.com/repo.git",
        constraints={"read_only": False},
        tools=[VIEW_FILE_TOOL_NAME],
    )
    prompt = build_system_prompt(request, tmp_path, tool_registry=DEFAULT_TOOL_REGISTRY)
    assert f"### `{VIEW_FILE_TOOL_NAME}`" in prompt
    assert "execute_bash" not in prompt


def test_build_system_prompt_includes_repo_scout_overlay(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Inspect debt",
        repo_url="https://example.com/repo.git",
        constraints={
            "task_type": "scout",
            "scout_mode": "repo",
            "max_proposals": 5,
        },
    )
    prompt = build_system_prompt(request, tmp_path)
    assert "## Scout Mode Guardrails" in prompt
    assert "up to 5 structured proposal(s)" in prompt
    assert "Return exactly one JSON object" in prompt
    assert "`implementation_slice`" in prompt
    assert "Mode: `repo`" in prompt
    assert "Focus on inspecting the local repository" in prompt


def test_build_system_prompt_includes_research_scout_overlay(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Research DB patterns",
        repo_url="https://example.com/repo.git",
        constraints={
            "task_type": "scout",
            "scout_mode": "research",
            "scout_focus": "SQLAlchemy migrations",
            "scout_depth": "deep",
            "max_proposals": 2,
        },
    )
    prompt = build_system_prompt(request, tmp_path)
    assert "## Scout Mode Guardrails" in prompt
    assert "up to 2 structured proposal(s)" in prompt
    assert "Mode: `research`" in prompt
    assert "Depth: `deep`" in prompt
    assert "Focus: SQLAlchemy migrations" in prompt
    assert "Pay close attention to the requested focus area" in prompt
    assert "Source Policy: use available/local evidence first" in prompt


def test_build_system_prompt_includes_deep_scout_overlay(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Deep dive on architecture",
        repo_url="https://example.com/repo.git",
        constraints={
            "task_type": "scout",
            "scout_mode": "deep",
        },
    )
    prompt = build_system_prompt(request, tmp_path)
    assert "## Scout Mode Guardrails" in prompt
    assert "up to 3 structured proposal(s)" in prompt
    assert "Mode: `deep`" in prompt
    assert "Use a repo-first, then targeted-research structure" in prompt
    assert "Source Policy: use available/local evidence first" in prompt


def test_build_system_prompt_omits_scout_overlay_for_normal_tasks(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Fix bug",
        repo_url="https://example.com/repo.git",
        constraints={
            "task_type": "feature",
            "scout_mode": "repo",  # Should be ignored if not scout
        },
    )
    prompt = build_system_prompt(request, tmp_path)
    assert "Scout Mode Guardrails" not in prompt


def test_build_system_prompt_ignores_non_string_scout_constraints(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Research DB patterns",
        repo_url="https://example.com/repo.git",
        constraints={
            "task_type": "scout",
            "scout_mode": "research",
            "scout_focus": ["not", "a", "string"],
            "scout_depth": {"invalid": "type"},
            "max_proposals": 2,
        },
    )
    prompt = build_system_prompt(request, tmp_path)
    assert "## Scout Mode Guardrails" in prompt
    assert "Mode: `research`" in prompt
    # Since depth and focus are invalid, they should be omitted
    assert "Depth:" not in prompt
    assert "Focus:" not in prompt


def test_build_system_prompt_renders_advisory_metadata(tmp_path) -> None:
    """Test that build_system_prompt formats memories with advisory strength metadata."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Run task",
        repo_url="https://example.com/repo.git",
        memory_context={
            "personal": [
                {
                    "memory_key": "communication_style",
                    "value": {"style": "concise"},
                    "confidence": 0.85,
                    "last_verified_at": "2026-07-04T12:00:00Z",
                    "requires_verification": False,
                    "gate_status": "accepted",
                    "risk": "low",
                    "advisory_strength": 0.85,
                }
            ],
            "project": [
                {
                    "memory_key": "verification_commands",
                    "value": {"command": "pytest"},
                    "confidence": 0.95,
                    "last_verified_at": None,
                    "requires_verification": True,
                    "gate_status": "advisory",
                    "risk": "low",
                    "advisory_strength": 0.665,
                }
            ],
        },
    )

    prompt = build_system_prompt(request, tmp_path)

    # Verify prepended warning warning
    assert "Memory context is advisory. Current user instructions" in prompt

    # Verify personal memory formatting has the metadata
    assert "communication_style" in prompt
    assert (
        "**communication_style** [accepted, risk=low, strength=0.85, verified=2026-07-04]:"
        in prompt
    )

    # Verify project memory formatting has the metadata
    assert "verification_commands" in prompt
    assert (
        "**verification_commands** [advisory, risk=low, strength=0.67, "
        "unverified, requires verification]:"
    ) in prompt


def test_build_system_prompt_renders_repository_profile_without_project_duplicates(
    tmp_path,
) -> None:
    """Repository profiles are advisory and replace raw project-memory rendering."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Run task",
        memory_context={
            "repository_profile": {
                "verification_commands": [
                    {
                        "memory_key": "verification_commands",
                        "value": {"command": "pytest"},
                        "gate_status": "accepted",
                        "advisory_strength": 0.9,
                    }
                ],
                "conventions": [],
                "pitfalls": [],
                "remembered_instructions": [],
                "general_facts": [],
            },
            "project": [
                {
                    "memory_key": "verification_commands",
                    "value": {"command": "pytest"},
                    "gate_status": "accepted",
                }
            ],
            "personal": [],
        },
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "### Repository Profile (Advisory)" in prompt
    assert "cannot change setup, validation, approval" in prompt
    assert prompt.count("**verification_commands**") == 1
    assert "### Project Memories" not in prompt


def test_build_system_prompt_omits_empty_repository_profile(tmp_path) -> None:
    """An empty shaped profile must not add a misleading repository section."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Run task",
        memory_context={
            "personal": [],
            "project": [],
            "repository_profile": {
                "verification_commands": [],
                "conventions": [],
                "pitfalls": [],
                "remembered_instructions": [],
                "general_facts": [],
            },
        },
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "Repository Profile" not in prompt
    assert "## Durable Memories" not in prompt


def test_build_system_prompt_keeps_personal_memory_without_empty_profile(tmp_path) -> None:
    """Personal memory remains visible when the repository profile has no items."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Run task",
        memory_context={
            "personal": [
                {
                    "memory_key": "communication_style",
                    "value": {"style": "concise"},
                }
            ],
            "project": [],
            "repository_profile": {
                "verification_commands": [],
                "conventions": [],
                "pitfalls": [],
                "remembered_instructions": [],
                "general_facts": [],
            },
        },
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "### Personal Memories" in prompt
    assert "Repository Profile" not in prompt


def test_build_system_prompt_ignores_malformed_repository_profile(tmp_path) -> None:
    """Legacy malformed profile values should degrade to no profile section."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Run task",
        memory_context={"repository_profile": ["legacy", "value"]},
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "Repository Profile" not in prompt


def test_build_system_prompt_does_not_orphan_items_after_budget_overflow(tmp_path) -> None:
    """Prompt truncation must not attach later sections to an earlier heading."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Run task",
        memory_context={
            "repository_profile": {
                "verification_commands": [
                    {
                        "memory_key": "large_command",
                        "value": {"command": "x" * 4000},
                    }
                ],
                "conventions": [
                    {
                        "memory_key": "later_convention",
                        "value": {"rule": "must not be orphaned"},
                    }
                ],
            }
        },
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "large_command" not in prompt
    assert "later_convention" not in prompt


def test_build_system_prompt_normalizes_memory_models_and_nullable_lists(tmp_path) -> None:
    """Live Pydantic context models and null collections render safely."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Run task",
        memory_context={
            "personal": [MemoryEntry(memory_key="personal_hint", value={"hint": "concise"})],
            "project": None,
            "repository_profile": RepositoryMemoryProfile(),
            "observations": [
                ObservationContextEntry(
                    id="obs-1",
                    observed_at=datetime.now(UTC),
                    source="worker",
                    event_type="completed",
                    summary="Observed completion.",
                )
            ],
        },
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "personal_hint" in prompt
    assert "Observed completion." in prompt


def test_build_system_prompt_advisory_metadata_handles_none_confidence(tmp_path) -> None:
    """Test that build_system_prompt safely formats memory metadata with a None confidence."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Run task",
        repo_url="https://example.com/repo.git",
        memory_context={
            "personal": [
                {
                    "memory_key": "communication_style",
                    "value": {"style": "concise"},
                    "confidence": None,
                    "last_verified_at": "2026-07-04T12:00:00Z",
                    "requires_verification": False,
                    "gate_status": "accepted",
                    "risk": "low",
                    "advisory_strength": 1.0,
                }
            ],
        },
    )

    prompt = build_system_prompt(request, tmp_path)
    assert "communication_style" in prompt
    assert (
        "**communication_style** [accepted, risk=low, strength=1.00, verified=2026-07-04]:"
        in prompt
    )


def test_build_system_prompt_sorts_memories_correctly(tmp_path) -> None:
    """Test sorting order of accepted/advisory memories in system prompt."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Run task",
        repo_url="https://example.com/repo.git",
        memory_context={
            "project": [
                {
                    "memory_key": "weak_proj",
                    "value": {"cmd": "weak"},
                    "confidence": 0.5,
                    "last_verified_at": "2026-07-01T12:00:00Z",
                    "requires_verification": True,
                    "gate_status": "advisory",
                    "risk": "medium",
                    "advisory_strength": 0.35,
                },
                {
                    "memory_key": "strong_proj",
                    "value": {"cmd": "strong"},
                    "confidence": 0.95,
                    "last_verified_at": "2026-07-02T12:00:00Z",
                    "requires_verification": False,
                    "gate_status": "accepted",
                    "risk": "low",
                    "advisory_strength": 0.95,
                },
            ],
        },
    )

    prompt = build_system_prompt(request, tmp_path)
    # The strong project memory should appear before the weak project memory
    strong_idx = prompt.index("strong_proj")
    weak_idx = prompt.index("weak_proj")
    assert strong_idx < weak_idx


def test_worker_prompt_to_dict_defensive_handling() -> None:
    class FailingDump:
        def model_dump(self) -> None:
            raise ValueError("model_dump failed")

        def dict(self) -> None:
            raise ValueError("dict failed")

    from utils.serialization import to_dict

    assert to_dict(FailingDump()) == {}


def test_worker_prompt_safe_float_defensive_handling() -> None:
    from workers.prompt_memory import _safe_float

    assert _safe_float(0.5) == 0.5
    assert _safe_float("0.25") == 0.25
    assert _safe_float(None, 2.0) == 2.0
    assert _safe_float("invalid", 1.5) == 1.5
    assert _safe_float([], 1.2) == 1.2


def test_build_memory_context_section_with_pydantic_model() -> None:
    from orchestrator.state import MemoryContext
    from workers.prompt_memory import build_memory_context_section

    request = WorkerRequest.model_construct(
        task_text="Run task",
        memory_context=MemoryContext(
            personal=[],
            project=[],
            repository_profile={
                "verification_commands": [],
                "conventions": [],
                "pitfalls": [],
                "remembered_instructions": [],
                "general_facts": [],
            },
        ),
    )
    assert build_memory_context_section(request) == ""
