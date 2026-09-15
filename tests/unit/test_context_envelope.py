"""Unit tests for M28.5D ContextEnvelope models, assembler, boundedness, digests, and redaction."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import time
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from orchestrator.context_envelope import (
    MAX_ENVELOPE_BYTES,
    ContextEnvelope,
    TruncationRecord,
    _apply_progressive_truncation,
    _bound_json_value,
    _bound_str,
    _bound_string_list,
    _compute_digests,
    _compute_worktree_state_digest,
    _detect_build_systems,
    _discover_repo_skills,
    _hash_git_status_stream,
    _project_intent_and_repo,
    _resolve_git_commit_sha,
    _safe_read_workspace_file,
    _stream_git_diff,
    _stream_git_status,
    _stream_hash_file,
    assemble_context_envelope,
    sanitize_context_envelope_metadata,
)
from orchestrator.state import MemoryContext, MemoryEntry, TaskSpec
from workers.base import ArtifactReference, SecretRef, WorkerRequest


def test_context_envelope_model_validation() -> None:
    """Validate ContextEnvelope requires schema_version=1, digests, and forbids extra fields."""
    env = ContextEnvelope(
        envelope_id="env-1",
        task_id="task-1",
        assembled_at="2026-09-13T22:00:00Z",
        context_content_digest="abc",
        evidence_digest="def",
        objective="Do the thing",
    )
    assert env.schema_version == 1
    assert env.objective == "Do the thing"
    assert env.dispatch_role == "primary"
    assert env.logical_attempt == 1

    with pytest.raises(Exception):
        ContextEnvelope(
            envelope_id="env-1",
            task_id="task-1",
            assembled_at="2026-09-13T22:00:00Z",
            context_content_digest="abc",
            evidence_digest="def",
            objective="Do the thing",
            extra_field="disallowed",
        )


def test_assemble_minimal_inputs() -> None:
    """Assembler should succeed with minimal inputs and fallback to task_text."""
    request = WorkerRequest(task_text="Refactor login flow")
    envelope, status = assemble_context_envelope(
        task_id="task-min-1",
        session_id=None,
        task_spec=None,
        task_text="Refactor login flow",
        memory_context=None,
        worker_request=request,
    )
    assert status == "assembled"
    assert envelope is not None
    assert envelope.task_id == "task-min-1"
    assert envelope.objective == "Refactor login flow"
    assert envelope.acceptance_criteria == []
    assert envelope.gated_memory_entries == []
    assert envelope.session_context.active_goal is None
    assert envelope.context_content_digest
    assert envelope.evidence_digest


def _setup_full_test_context(
    tmp_path: Path,
) -> tuple[TaskSpec, WorkerRequest, MemoryContext, dict[str, Any]]:
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname = 'demo'")
    (tmp_path / "AGENTS.md").write_text("# Project Guidance\nUse Poetry.\n")
    skills_dir = tmp_path / ".agents" / "skills" / "demo-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("---\nname: Demo Skill\ndescription: A test skill\n---\n")

    task_spec = TaskSpec(
        goal="Add feature X",
        acceptance_criteria=["Tests pass", "Documentation updated"],
        assumptions=["Python 3.12"],
        non_goals=["No breaking changes"],
        verification_commands=["pytest tests/"],
        allowed_actions=["modify_workspace_files"],
    )
    request = WorkerRequest(
        task_id="task-full-1",
        task_text="Add feature X",
        repo_url="https://github.com/example/repo",
        branch="main",
        workspace_id="ws-123",
        secret_refs=(SecretRef(name="github_token"),),
    )
    mem_ctx = MemoryContext()
    mem_ctx.project.append(
        MemoryEntry(
            memory_key="arch_note",
            value={"note": "Use async"},
            source="test",
            confidence=0.95,
            scope="repo",
            gate_status="accepted",
            advisory_strength=0.9,
            staleness=0.1,
            gate_reason_codes=["high_confidence"],
        )
    )
    mem_ctx.personal.append(
        MemoryEntry(
            memory_key="stale_pref",
            value={"theme": "dark"},
            gate_status="suppressed",
        )
    )
    mem_ctx.session = {
        "active_goal": "Migrate DB",
        "decisions_made": {"orm": "SQLAlchemy"},
        "identified_risks": {"downtime": "low"},
        "files_touched": ["db/models.py", "db/enums.py"],
    }
    prior_nodes = {
        "node-0": {
            "summary": "Prepared schema",
            "status": "completed",
            "files_changed": ["db/schema.sql"],
            "artifacts": [{"name": "diff.patch"}],
        }
    }
    return task_spec, request, mem_ctx, prior_nodes


def test_assemble_full_inputs(tmp_path: Path) -> None:
    """Assembler should populate all sections, repo facts, memory, and session context."""
    task_spec, request, mem_ctx, prior_nodes = _setup_full_test_context(tmp_path)

    envelope, status = assemble_context_envelope(
        task_id="task-full-1",
        session_id="sess-456",
        node_id="node-1",
        dispatch_role="decomposed_node",
        logical_attempt=2,
        task_spec=task_spec,
        task_text="Add feature X",
        memory_context=mem_ctx,
        worker_request=request,
        prior_node_context=prior_nodes,
        workspace_path=tmp_path,
    )
    assert status == "assembled"
    assert envelope is not None
    assert envelope.task_id == "task-full-1"
    assert envelope.node_id == "node-1"
    assert envelope.dispatch_role == "decomposed_node"
    assert envelope.logical_attempt == 2
    assert envelope.objective == "Add feature X"
    assert envelope.acceptance_criteria == ["Tests pass", "Documentation updated"]
    assert envelope.verification_plan == ["pytest tests/"]
    assert envelope.repo_facts.has_agents_md is True
    assert "poetry/pyproject" in envelope.repo_facts.detected_build_systems
    assert envelope.repo_instructions_snapshot == "# Project Guidance\nUse Poetry."
    assert len(envelope.repo_skills) == 1
    assert envelope.repo_skills[0].name == "Demo Skill"
    assert envelope.repo_skills[0].description == "A test skill"
    assert len(envelope.gated_memory_entries) == 1
    assert envelope.gated_memory_entries[0].memory_key == "arch_note"
    assert envelope.session_context.active_goal == "Migrate DB"
    assert envelope.session_context.decisions_made == {"orm": "SQLAlchemy"}
    assert len(envelope.dependency_outputs) == 1
    assert envelope.dependency_outputs[0].node_id == "node-0"
    assert envelope.capability_summary.granted_secret_refs == ["github_token"]

    # Selected references should deduplicate session and dependency files
    paths = [r.path for r in envelope.selected_references]
    assert "db/models.py" in paths
    assert "db/schema.sql" in paths


def test_digest_stability_on_resubmission() -> None:
    """Resubmitting identical task content produces identical context_content_digest."""
    req1 = WorkerRequest(task_text="Run benchmarks")
    env1, _ = assemble_context_envelope(
        task_id="task-run-1",
        session_id="sess-A",
        task_spec=None,
        task_text="Run benchmarks",
        memory_context=None,
        worker_request=req1,
    )
    req2 = WorkerRequest(task_text="Run benchmarks")
    env2, _ = assemble_context_envelope(
        task_id="task-run-2",
        session_id="sess-B",
        task_spec=None,
        task_text="Run benchmarks",
        memory_context=None,
        worker_request=req2,
    )
    assert env1 is not None and env2 is not None
    # Semantic digest matches
    assert env1.context_content_digest == env2.context_content_digest
    # Audit evidence digest differs because task_id / session_id differ
    assert env1.evidence_digest != env2.evidence_digest


def test_secret_redaction_canaries() -> None:
    """Canary test verifying known legacy secrets are redacted across all free-form fields."""
    secret_val = "ghp_superSecretToken12345"
    task_spec = TaskSpec(
        goal=f"Fix bug using {secret_val}",
        acceptance_criteria=[f"Pass with {secret_val}"],
        assumptions=[f"Assume {secret_val}"],
        non_goals=[f"No {secret_val}"],
        verification_commands=[f"check --token={secret_val}"],
    )
    request = WorkerRequest(
        task_text=f"Fix bug with {secret_val}",
        secrets={"GITHUB_TOKEN": secret_val},
    )
    mem_ctx = MemoryContext()
    mem_ctx.project.append(
        MemoryEntry(
            memory_key="leak_key",
            value={"token": secret_val},
            gate_status="accepted",
        )
    )
    mem_ctx.session = {
        "active_goal": f"Goal {secret_val}",
        "decisions_made": {"auth": secret_val},
        "identified_risks": {"risk": secret_val},
    }
    prior_nodes = {"node-0": {"summary": f"Summary {secret_val}"}}

    envelope, status = assemble_context_envelope(
        task_id="task-canary",
        session_id="sess-canary",
        task_spec=task_spec,
        task_text=f"Fix bug with {secret_val}",
        memory_context=mem_ctx,
        worker_request=request,
        prior_node_context=prior_nodes,
    )
    assert status == "assembled"
    assert envelope is not None

    dumped = json.dumps(envelope.model_dump(mode="json"))
    assert secret_val not in dumped
    assert "[REDACTED]" in dumped


def test_boundedness_and_truncation_records() -> None:
    """Verify string list caps, repo instructions cap, and truncation records."""
    long_items = [f"item_{i}" for i in range(50)]
    truncations: list[TruncationRecord] = []
    bounded = _bound_string_list(
        long_items, max_items=10, max_chars=100, field="test_list", truncations=truncations
    )
    assert len(bounded) == 10
    assert len(truncations) == 1
    assert truncations[0].field == "test_list_count"
    assert truncations[0].original_length == 50
    assert truncations[0].truncated_length == 10

    long_str = "x" * 5000
    trunc_str = _bound_str(long_str, max_chars=100, field="long_str", truncations=truncations)
    assert len(trunc_str) <= 100
    assert trunc_str.endswith("... (truncated)")


def test_recursive_json_bounding() -> None:
    """Verify deep nesting and oversized dict keys are constrained."""
    nested: dict[str, Any] = {"a": {"b": {"c": {"d": {"e": {"f": "too deep"}}}}}}
    bounded = _bound_json_value(nested, max_depth=3)
    assert bounded["a"]["b"]["c"] == "<nested_depth_exceeded>"

    wide_dict = {f"k{i}": "val" for i in range(100)}
    bounded_dict = _bound_json_value(wide_dict, max_keys=20)
    assert len(bounded_dict) == 20


def test_oversized_envelope_omitted() -> None:
    """An envelope exceeding 256 KiB after progressive stages is omitted with status."""
    huge_mem_ctx = MemoryContext()
    for i in range(200):
        huge_mem_ctx.project.append(
            MemoryEntry(
                memory_key=f"huge_{i}",
                value={"blob": "x" * 2000},
                gate_status="accepted",
                advisory_strength=1.0,
            )
        )
    request = WorkerRequest(task_text="Handle huge memory")
    envelope, status = assemble_context_envelope(
        task_id="task-huge",
        session_id=None,
        task_spec=None,
        task_text="Handle huge memory",
        memory_context=huge_mem_ctx,
        worker_request=request,
    )
    # If the payload remains > 256 KiB after all stages, it must be omitted
    if status == "omitted_oversize":
        assert envelope is None
    else:
        assert status == "assembled"
        assert envelope is not None
        payload_bytes = len(json.dumps(envelope.model_dump(mode="json")).encode("utf-8"))
        assert payload_bytes <= MAX_ENVELOPE_BYTES


def test_url_credentials_masked_in_envelope() -> None:
    """Verify repo_url with embedded credentials is masked before persisting/digesting."""
    request = WorkerRequest(
        task_text="Run with private repo",
        repo_url="https://alice:supersecret@example.com/repo.git",
        secrets={},
    )
    envelope, status = assemble_context_envelope(
        task_id="task-cred-1",
        session_id=None,
        task_spec=None,
        task_text="Run with private repo",
        memory_context=None,
        worker_request=request,
    )
    assert status == "assembled"
    assert envelope is not None
    dumped = json.dumps(envelope.model_dump(mode="json"))
    assert "supersecret" not in dumped
    assert "alice" not in dumped
    assert "https://****@example.com/repo.git" in dumped
    assert envelope.repo_facts.repo_url == "https://****@example.com/repo.git"


def test_hard_oversize_envelope_omitted_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that if serialized envelope exceeds MAX_ENVELOPE_BYTES,
    assemble_context_envelope returns omitted_oversize."""
    import orchestrator.context_envelope as ce

    monkeypatch.setattr(ce, "MAX_ENVELOPE_BYTES", 200)
    request = WorkerRequest(task_text="Test boundary limit")
    envelope, status = assemble_context_envelope(
        task_id="task-small-limit",
        session_id=None,
        task_spec=None,
        task_text="Test boundary limit",
        memory_context=None,
        worker_request=request,
    )
    assert status == "omitted_oversize"
    assert envelope is None


def test_digest_stability_across_workspaces() -> None:
    """Identical task in workspace-a vs workspace-b yields identical context_content_digest."""
    req1 = WorkerRequest(task_text="Run benchmarks", workspace_id="ws-alpha")
    env1, _ = assemble_context_envelope(
        task_id="task-run-1",
        session_id="sess-A",
        task_spec=None,
        task_text="Run benchmarks",
        memory_context=None,
        worker_request=req1,
    )
    req2 = WorkerRequest(task_text="Run benchmarks", workspace_id="ws-beta")
    env2, _ = assemble_context_envelope(
        task_id="task-run-2",
        session_id="sess-B",
        task_spec=None,
        task_text="Run benchmarks",
        memory_context=None,
        worker_request=req2,
    )
    assert env1 is not None and env2 is not None
    # Semantic digest matches despite differing workspace_ids
    assert env1.context_content_digest == env2.context_content_digest
    # Audit evidence digest differs because workspace_id / session_id differ
    assert env1.evidence_digest != env2.evidence_digest


def test_task_spec_risk_level_precedence() -> None:
    """task_spec.risk_level authoritatively determines capability_summary.risk_level."""
    task_spec = TaskSpec(goal="Dangerous operation", risk_level="critical")
    req = WorkerRequest(
        task_text="Dangerous operation",
        constraints={"risk_level": "low"},
    )
    env, _ = assemble_context_envelope(
        task_id="task-risk-1",
        session_id=None,
        task_spec=task_spec,
        task_text="Dangerous operation",
        memory_context=None,
        worker_request=req,
    )
    assert env is not None
    assert env.capability_summary.risk_level == "critical"


def test_progressive_truncation_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test all 4 progressive truncation fallback stages when payload exceeds ceiling."""
    import orchestrator.context_envelope as ce

    monkeypatch.setattr(ce, "MAX_ENVELOPE_BYTES", 3000)
    tr: list[TruncationRecord] = []
    data: dict[str, Any] = {
        "repo_instructions_snapshot": "A" * 5000,
        "gated_memory_entries": [{"key": f"k{i}", "val": "v"} for i in range(50)],
        "dependency_outputs": [{"summary": "S" * 1000} for _ in range(5)],
        "gate_diagnostics_summary": {"details": "diag" * 50},
    }
    _apply_progressive_truncation(data, tr)
    assert len(data["repo_instructions_snapshot"]) <= 2020
    assert any(t.field == "repo_instructions_snapshot_stage1" for t in tr)
    assert len(data["gated_memory_entries"]) <= 25
    assert any(t.field == "gated_memory_entries_stage2" for t in tr)
    assert any("summary_stage3" in t.field for t in tr)
    assert data["gate_diagnostics_summary"] == {}
    assert any(t.field == "gate_diagnostics_summary_stage4" for t in tr)


def test_discover_repo_skills_handles_unreadable_or_corrupt_file(tmp_path: Path) -> None:
    """_discover_repo_skills handles invalid/corrupt skill directories without raising."""
    skills_dir = tmp_path / ".agents" / "skills" / "bad_skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("--- invalid yaml: [", encoding="utf-8")
    tr: list[TruncationRecord] = []
    skills = _discover_repo_skills(tmp_path, tr)
    assert len(skills) == 1
    assert skills[0].name == "bad_skill"


def test_assemble_context_envelope_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """assemble_context_envelope returns (None, 'failed') on unexpected exception."""
    import orchestrator.context_envelope as ce

    monkeypatch.setattr(ce, "_project_intent_and_repo", lambda *args, **kwargs: 1 / 0)
    req = WorkerRequest(task_text="Crashing request")
    env, status = assemble_context_envelope(
        task_id="task-err-1",
        session_id=None,
        task_spec=None,
        task_text="Crashing request",
        memory_context=None,
        worker_request=req,
    )
    assert status == "failed"
    assert env is None


def test_detect_build_systems_all_markers(tmp_path: Path) -> None:
    """_detect_build_systems detects all supported build system markers."""
    (tmp_path / "Dockerfile").touch()
    (tmp_path / "package.json").touch()
    gh = tmp_path / ".github" / "workflows"
    gh.mkdir(parents=True)
    systems = _detect_build_systems(tmp_path)
    assert "docker" in systems
    assert "npm" in systems
    assert "github_actions" in systems


def test_discover_repo_skills_truncation(tmp_path: Path) -> None:
    """_discover_repo_skills truncates when exceeding MAX_SKILLS."""
    skills_root = tmp_path / ".agents" / "skills"
    for i in range(35):
        sdir = skills_root / f"skill_{i:02d}"
        sdir.mkdir(parents=True)
        (sdir / "SKILL.md").write_text(f"---\nname: Skill {i}\n---\n", encoding="utf-8")
    tr: list[TruncationRecord] = []
    skills = _discover_repo_skills(tmp_path, tr)
    assert len(skills) == 30
    assert any(t.field == "repo_skills" for t in tr)


def test_safe_read_workspace_file_symlink_rejection(tmp_path: Path) -> None:
    """_safe_read_workspace_file rejects symlinks and files outside workspace."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("sensitive secret data")

    symlink_file = ws / "AGENTS.md"
    symlink_file.symlink_to(outside)

    content, orig_size, has_overflow = _safe_read_workspace_file(ws, symlink_file, 1000)
    assert content is None
    assert orig_size == 0
    assert has_overflow is False

    req = WorkerRequest(task_text="test")
    tr: list[TruncationRecord] = []
    _intent, repo_facts, snap, _skills = _project_intent_and_repo(
        task_spec=None, task_text="test", worker_request=req, workspace_path=ws, truncations=tr
    )
    assert repo_facts["has_agents_md"] is False
    assert snap is None


def test_safe_read_workspace_file_bounded_reads(tmp_path: Path) -> None:
    """_safe_read_workspace_file bounds stream reads to max_chars + 1."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    large_file = ws / "large.txt"
    large_file.write_text("x" * 5000)

    content, orig_size, has_overflow = _safe_read_workspace_file(ws, large_file, 100)
    assert content == "x" * 100
    assert orig_size == 5000
    assert has_overflow is True

    small_file = ws / "small.txt"
    small_file.write_text("hello world")
    c2, orig_size2, has_overflow2 = _safe_read_workspace_file(ws, small_file, 100)
    assert c2 == "hello world"
    assert orig_size2 == 11
    assert has_overflow2 is False


def test_discover_repo_skills_rejects_symlinked_skill_dir(tmp_path: Path) -> None:
    """_discover_repo_skills rejects symlinked skill folders pointing outside."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside_skill = tmp_path / "evil_skill"
    outside_skill.mkdir()
    (outside_skill / "SKILL.md").write_text("---\nname: Evil\n---\n")

    skills_dir = ws / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    symlink_skill = skills_dir / "evil_skill"
    symlink_skill.symlink_to(outside_skill)

    tr: list[TruncationRecord] = []
    skills = _discover_repo_skills(ws, tr)
    assert len(skills) == 0


def test_resolve_git_commit_sha_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Git fallback resolves HEAD without logging a potentially sensitive path."""
    import subprocess

    sensitive_path_segment = "api-key-super-secret"
    ws = tmp_path / sensitive_path_segment / "repo"
    ws.mkdir(parents=True)
    assert _resolve_git_commit_sha(ws) is None

    git_dir = ws / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    refs_dir = git_dir / "refs" / "heads"
    refs_dir.mkdir(parents=True)
    sha_value = "a" * 40
    (refs_dir / "main").write_text(f"{sha_value}\n")

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("no git")),
    )
    with caplog.at_level("DEBUG", logger="orchestrator.context_envelope"):
        resolved = _resolve_git_commit_sha(ws)
    assert resolved == sha_value
    assert "Failed to run git rev-parse" in caplog.text
    assert sensitive_path_segment not in caplog.text


def test_semantic_digest_changes_on_commit_sha(tmp_path: Path) -> None:
    """context_content_digest differs when git commit SHA changes."""
    ws1 = tmp_path / "ws1"
    ws1.mkdir()

    sha1 = "1111111111111111111111111111111111111111"
    sha2 = "2222222222222222222222222222222222222222"

    req = WorkerRequest(task_text="test task", repo_url="https://github.com/ex/repo")
    env1, _ = assemble_context_envelope(
        task_id="t1",
        session_id=None,
        task_spec=None,
        task_text="test task",
        memory_context=None,
        worker_request=req,
        workspace_path=ws1,
    )
    assert env1 is not None

    env1_mod = env1.model_copy(
        update={"repo_facts": env1.repo_facts.model_copy(update={"commit_sha": sha1})}
    )
    d1_ctx, _ = _compute_digests(env1_mod.model_dump(mode="json"))

    env2_mod = env1.model_copy(
        update={"repo_facts": env1.repo_facts.model_copy(update={"commit_sha": sha2})}
    )
    d2_ctx, _ = _compute_digests(env2_mod.model_dump(mode="json"))

    assert d1_ctx != d2_ctx


def test_evidence_digest_binds_context_content_digest() -> None:
    """evidence_digest depends directly on context_content_digest."""
    from orchestrator.context_envelope import _VOLATILE_EVIDENCE_KEYS, _compute_canonical_bytes

    data = {"context_content_digest": "sha_one", "objective": "test"}
    b1 = _compute_canonical_bytes(data, _VOLATILE_EVIDENCE_KEYS)
    data["context_content_digest"] = "sha_two"
    b2 = _compute_canonical_bytes(data, _VOLATILE_EVIDENCE_KEYS)
    assert b1 != b2
    assert "context_content_digest" not in _VOLATILE_EVIDENCE_KEYS


def test_envelope_deep_copy_isolation() -> None:
    """Mutations to request.context_envelope do not affect artifact_metadata."""
    import copy

    req = WorkerRequest(task_text="isolation test", branch="main")
    env, _ = assemble_context_envelope(
        task_id="t1",
        session_id=None,
        task_spec=None,
        task_text="isolation test",
        memory_context=None,
        worker_request=req,
    )
    assert env is not None
    envelope_dump = env.model_dump(mode="json")
    req = req.model_copy(update={"context_envelope": copy.deepcopy(envelope_dump)})
    art = ArtifactReference(
        name="context_envelope",
        uri=f"envelope://{env.envelope_id}",
        artifact_type="context_envelope",
        artifact_metadata=copy.deepcopy(envelope_dump),
    )

    assert req.context_envelope is not None
    assert art.artifact_metadata is not None
    req.context_envelope["repo_facts"]["branch"] = "mutated_branch"
    assert art.artifact_metadata["repo_facts"]["branch"] == "main"


def test_context_envelope_coverage_branches(tmp_path: Path) -> None:
    """Exercise boundary and error branches in context_envelope."""
    import subprocess
    import unittest.mock as mock

    from orchestrator.context_envelope import (
        _bound_json_value,
        _bound_string_list,
        _extract_gated_memories,
        _project_context_sections,
    )

    tr: list[TruncationRecord] = []
    # _bound_string_list and _bound_json_value
    assert _bound_string_list(["a" * 20], 5, 5, "f", tr) == ["a" * 5]
    assert _bound_json_value("x" * 20, max_str=5, field="str_f", truncations=tr) == "x" * 5
    assert _bound_json_value([1, 2, 3, 4], max_list=2, field="list_f", truncations=tr) == [1, 2]
    assert _bound_json_value(42) == 42

    # _safe_read_workspace_file with None workspace and OSError
    c, s, of = _safe_read_workspace_file(None, tmp_path / "foo", 100)
    assert c is None and s == 0 and of is False

    ws = tmp_path / "ws_err"
    ws.mkdir()
    f_err = ws / "locked.txt"
    f_err.write_text("hello")
    with mock.patch.object(Path, "open", side_effect=OSError("denied")):
        c_err, s_err, of_err = _safe_read_workspace_file(ws, f_err, 100)
        assert c_err is None and s_err == 0 and of_err is False

    # Git commit sha with timeout and missing ref
    git_dir = ws / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/missing\n")
    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1)):
        assert _resolve_git_commit_sha(ws) is None

    # Oversized AGENTS.md in workspace
    ws_large = tmp_path / "ws_large_agents"
    ws_large.mkdir()
    (ws_large / "AGENTS.md").write_text("Instruction line.\n" * 400)
    tr_large: list[TruncationRecord] = []
    _intent, _facts, snap, _skills = _project_intent_and_repo(
        task_spec=None,
        task_text="test",
        worker_request=WorkerRequest(task_text="test"),
        workspace_path=ws_large,
        truncations=tr_large,
    )
    assert snap is not None and snap.endswith("... (truncated)")

    # _extract_gated_memories with None, suppressed, and truncation
    assert _extract_gated_memories(None, []) == []
    mem_ctx = MemoryContext()
    mem_ctx.project.append(
        MemoryEntry(memory_key="supp", value={"note": "supp"}, gate_status="suppressed")
    )
    for i in range(110):
        mem_ctx.project.append(
            MemoryEntry(memory_key=f"k{i}", value={"v": i}, gate_status="accepted")
        )
    extracted = _extract_gated_memories(mem_ctx, tr)
    assert len(extracted) == 50
    assert any(t.field == "gated_memory_entries" for t in tr)

    # _project_context_sections with oversized files_touched and selected_references
    mem_ctx.session = {"files_touched": [f"f_{i}.py" for i in range(250)]}
    deps = [{"summary": "dep", "files_changed": [f"dep_{i}.py" for i in range(120)]}]
    s_ctx, _cap, refs = _project_context_sections(
        mem_ctx, WorkerRequest(task_text="test"), None, deps, tr
    )
    assert len(s_ctx["files_touched"]) == 200
    assert len(refs) == 200
    assert any(t.field == "selected_references" for t in tr)


def test_digests_reproducible_from_persisted_envelope_with_datetimes() -> None:
    """Recomputing digests from persisted payload (JSON-serialized) matches computed digests."""
    from datetime import datetime

    verified_dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    mem_ctx = MemoryContext(
        project=[
            MemoryEntry(
                memory_key="repo_convention",
                value={"rule": "always type hints"},
                last_verified_at=verified_dt,
                requires_verification=False,
                gate_status="accepted",
                advisory_strength=1.0,
            )
        ]
    )
    req = WorkerRequest(task_text="Check reproducible digests with datetime")
    env, status = assemble_context_envelope(
        task_id="task-dt-1",
        session_id="s1",
        task_spec=None,
        task_text="Check reproducible digests with datetime",
        memory_context=mem_ctx,
        worker_request=req,
    )
    assert status == "assembled"
    assert env is not None

    persisted_json = env.model_dump(mode="json")
    recomputed_cd, recomputed_ed = _compute_digests(persisted_json)

    assert recomputed_cd == persisted_json["context_content_digest"]
    assert recomputed_ed == persisted_json["evidence_digest"]


def test_resolve_git_commit_sha_trusted_mismatch(tmp_path: Path) -> None:
    """_resolve_git_commit_sha prioritizes trusted git dir over worker-controlled workspace .git."""
    ws = tmp_path / "workspaces" / "ws_123"
    ws.mkdir(parents=True)
    ws_git = ws / ".git"
    ws_git.mkdir()
    forged_sha = "1111111111111111111111111111111111111111"
    (ws_git / "HEAD").write_text(f"{forged_sha}\n")

    trusted_git = tmp_path / "workspaces" / ".code-agent-git" / "ws_123"
    trusted_git.mkdir(parents=True)
    trusted_sha = "2222222222222222222222222222222222222222"
    (trusted_git / "HEAD").write_text(f"{trusted_sha}\n")

    # With workspace_id passed, broker-authoritative dir is auto-discovered
    resolved = _resolve_git_commit_sha(ws, workspace_id="ws_123")
    assert resolved == trusted_sha

    # With explicit trusted_git_dir passed, it is preferred
    custom_trusted = tmp_path / "custom_git"
    custom_trusted.mkdir()
    custom_sha = "3333333333333333333333333333333333333333"
    (custom_trusted / "HEAD").write_text(f"{custom_sha}\n")
    resolved_custom = _resolve_git_commit_sha(ws, trusted_git_dir=custom_trusted)
    assert resolved_custom == custom_sha


def test_exact_4000_char_and_multibyte_agents_md_not_truncated(tmp_path: Path) -> None:
    """Exact 4000-char and multibyte UTF-8 AGENTS.md files are not falsely truncated."""
    ws_exact = tmp_path / "ws_exact"
    ws_exact.mkdir()
    exact_4000 = "a" * 4000
    (ws_exact / "AGENTS.md").write_text(exact_4000, encoding="utf-8")

    tr1: list[TruncationRecord] = []
    _intent1, _facts1, snap1, _skills1 = _project_intent_and_repo(
        task_spec=None,
        task_text="test",
        worker_request=WorkerRequest(task_text="test"),
        workspace_path=ws_exact,
        truncations=tr1,
    )
    assert snap1 == exact_4000
    assert not any(t.field == "repo_instructions_snapshot" for t in tr1)

    # Multibyte: 2000 4-byte characters (8000 bytes > 4000, but 2000 chars <= 4000)
    ws_multi = tmp_path / "ws_multi"
    ws_multi.mkdir()
    multi_chars = "🎉" * 2000
    (ws_multi / "AGENTS.md").write_text(multi_chars, encoding="utf-8")

    tr2: list[TruncationRecord] = []
    _intent2, _facts2, snap2, _skills2 = _project_intent_and_repo(
        task_spec=None,
        task_text="test",
        worker_request=WorkerRequest(task_text="test"),
        workspace_path=ws_multi,
        truncations=tr2,
    )
    assert snap2 == multi_chars
    assert not any(t.field == "repo_instructions_snapshot" for t in tr2)

    # Overflow: 4001 characters
    ws_over = tmp_path / "ws_over"
    ws_over.mkdir()
    over_4000 = "x" * 4001
    (ws_over / "AGENTS.md").write_text(over_4000, encoding="utf-8")

    tr3: list[TruncationRecord] = []
    _intent3, _facts3, snap3, _skills3 = _project_intent_and_repo(
        task_spec=None,
        task_text="test",
        worker_request=WorkerRequest(task_text="test"),
        workspace_path=ws_over,
        truncations=tr3,
    )
    assert snap3 is not None and snap3.endswith("... (truncated)")
    assert any(t.field == "repo_instructions_snapshot" for t in tr3)


def test_skill_truncation_records_description_and_source_file(tmp_path: Path) -> None:
    """Skill discovery records TruncationRecords for description and file overflow."""
    ws = tmp_path / "ws_skills"
    ws.mkdir()
    skill_dir = ws / ".agents" / "skills" / "giant_skill"
    skill_dir.mkdir(parents=True)
    long_desc = "D" * 250
    header = f"---\nname: giant_skill\ndescription: {long_desc}\n---\n"
    content = header + ("x" * 9000)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    tr: list[TruncationRecord] = []
    skills = _discover_repo_skills(ws, tr)
    assert len(skills) == 1
    skill = skills[0]
    assert skill.name == "giant_skill"
    assert skill.description is not None
    assert skill.description.endswith("... (truncated)")
    assert len(skill.description) <= 200

    desc_trunc = [t for t in tr if t.field == "repo_skills[giant_skill].description"]
    assert len(desc_trunc) == 1
    assert desc_trunc[0].original_length == 250
    assert desc_trunc[0].truncated_length == 200

    file_trunc = [t for t in tr if t.field == "repo_skills[giant_skill].source_file"]
    assert len(file_trunc) == 1
    assert file_trunc[0].original_length > 8192
    assert file_trunc[0].truncated_length == 8192


def _setup_git_worktree(tmp_path: Path, ws_name: str) -> tuple[Path, Path]:
    """Helper to setup a bare trusted git repo and matching worktree with initial commit."""
    trusted_git = tmp_path / ".code-agent-git" / ws_name
    ws = tmp_path / ws_name
    ws.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(trusted_git)], check=True, capture_output=True)
    (ws / "app.py").write_text("print('initial')", encoding="utf-8")
    subprocess.run(
        ["git", "--git-dir", str(trusted_git), "--work-tree", str(ws), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(trusted_git),
            "--work-tree",
            str(ws),
            "-c",
            "user.name=test",
            "-c",
            "user.email=t@e.com",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    return trusted_git, ws


def test_worktree_state_digest_tracks_retained_mutations(tmp_path: Path) -> None:
    """Mutating a tracked file in a retained workspace changes worktree and semantic digest."""
    trusted_git, ws = _setup_git_worktree(tmp_path, "ws_retained")
    wreq = WorkerRequest(task_text="Run repair", workspace_id="ws_retained")

    env_clean, status1 = assemble_context_envelope(
        task_id="t1",
        session_id="s1",
        task_spec=TaskSpec(goal="Repair task"),
        task_text="Run repair",
        memory_context=None,
        worker_request=wreq,
        workspace_path=ws,
        trusted_git_dir=trusted_git,
    )
    assert status1 == "assembled"
    assert env_clean is not None
    assert env_clean.repo_facts.commit_sha is not None
    assert env_clean.repo_facts.worktree_state_digest == hashlib.sha256(b"clean").hexdigest()
    assert env_clean.repo_facts.git_evidence_status == "complete"
    assert env_clean.repo_facts.git_evidence_reason is None

    # Mutate tracked file
    (ws / "app.py").write_text("print('repair mutation 1')", encoding="utf-8")
    env_mut1, status2 = assemble_context_envelope(
        task_id="t1",
        session_id="s1",
        task_spec=TaskSpec(goal="Repair task"),
        task_text="Run repair",
        memory_context=None,
        worker_request=wreq,
        workspace_path=ws,
        trusted_git_dir=trusted_git,
    )
    assert status2 == "assembled"
    assert env_mut1 is not None
    assert env_mut1.repo_facts.commit_sha == env_clean.repo_facts.commit_sha
    assert env_mut1.repo_facts.worktree_state_digest != env_clean.repo_facts.worktree_state_digest
    assert env_mut1.context_content_digest != env_clean.context_content_digest

    # Mutate again with different content
    (ws / "app.py").write_text("print('repair mutation 2')", encoding="utf-8")
    env_mut2, _ = assemble_context_envelope(
        task_id="t1",
        session_id="s1",
        task_spec=TaskSpec(goal="Repair task"),
        task_text="Run repair",
        memory_context=None,
        worker_request=wreq,
        workspace_path=ws,
        trusted_git_dir=trusted_git,
    )
    assert env_mut2 is not None
    assert env_mut2.repo_facts.worktree_state_digest != env_mut1.repo_facts.worktree_state_digest
    assert env_mut2.context_content_digest != env_mut1.context_content_digest


def test_worktree_state_digest_tracks_untracked_files(tmp_path: Path) -> None:
    """Creating and modifying untracked files alters worktree and semantic digest."""
    trusted_git, ws = _setup_git_worktree(tmp_path, "ws_untracked")
    wreq = WorkerRequest(task_text="Run task", workspace_id="ws_untracked")

    (ws / "scratch.txt").write_text("scratch note v1", encoding="utf-8")
    env1, _ = assemble_context_envelope(
        task_id="t1",
        session_id="s1",
        task_spec=TaskSpec(goal="Task"),
        task_text="Run task",
        memory_context=None,
        worker_request=wreq,
        workspace_path=ws,
        trusted_git_dir=trusted_git,
    )
    assert env1 is not None
    assert env1.repo_facts.worktree_state_digest != hashlib.sha256(b"clean").hexdigest()

    (ws / "scratch.txt").write_text("scratch note v2 altered", encoding="utf-8")
    env2, _ = assemble_context_envelope(
        task_id="t1",
        session_id="s1",
        task_spec=TaskSpec(goal="Task"),
        task_text="Run task",
        memory_context=None,
        worker_request=wreq,
        workspace_path=ws,
        trusted_git_dir=trusted_git,
    )
    assert env2 is not None
    assert env2.repo_facts.worktree_state_digest != env1.repo_facts.worktree_state_digest
    assert env2.context_content_digest != env1.context_content_digest


def test_compute_worktree_state_digest_nonexistent_workspace() -> None:
    """_compute_worktree_state_digest returns None for non-existent workspace."""
    assert _compute_worktree_state_digest(Path("/nonexistent/path")) is None
    assert _compute_worktree_state_digest(None) is None


def test_context_envelope_exposes_missing_git_evidence(tmp_path: Path) -> None:
    """Assembled envelopes explicitly identify unavailable Git provenance."""
    ws = tmp_path / "not-a-repository"
    ws.mkdir()
    env, status = assemble_context_envelope(
        task_id="t-missing-git",
        session_id="s1",
        task_spec=TaskSpec(goal="Inspect workspace"),
        task_text="Inspect workspace",
        memory_context=None,
        worker_request=WorkerRequest(task_text="Inspect workspace"),
        workspace_path=ws,
    )

    assert status == "assembled"
    assert env is not None
    assert env.repo_facts.git_evidence_status == "missing_git_repository"
    assert env.repo_facts.git_evidence_reason == "no trusted Git directory was available"


def test_unknown_credential_keys_redacted_in_envelope() -> None:
    """Unknown secrets under credential-shaped keys are replaced with [REDACTED]."""
    wreq = WorkerRequest(
        task_text="Perform task with sensitive memory",
        secret_refs=[SecretRef(name="ALLOWED_TOKEN")],
        secrets={"registered_token": "known-secret-val"},
    )
    mem_ctx = MemoryContext(
        project=[
            MemoryEntry(
                memory_key="db_config",
                value={
                    "host": "db.internal",
                    "api_key": "unregistered-super-secret-value",
                    "auth_token": "unknown-token-12345",
                    "db_password": "super-secret-pw",
                    "accessToken": "camel-access-token",
                    "apiKey": "camel-api-key",
                    "clientSecret": "camel-client-secret",
                    "notes": (
                        "apiKey=free-form-api-key Authorization: Bearer free-form-bearer "
                        + "ghp_"
                        + ("x" * 24)
                    ),
                },
                source="vault",
                gate_status="accepted",
            )
        ]
    )
    env, status = assemble_context_envelope(
        task_id="t_cred",
        session_id="s1",
        task_spec=TaskSpec(goal="Task"),
        task_text="Perform task",
        memory_context=mem_ctx,
        worker_request=wreq,
    )
    assert status == "assembled"
    assert env is not None
    assert len(env.gated_memory_entries) == 1
    mem_val = env.gated_memory_entries[0].value
    assert mem_val["host"] == "db.internal"
    assert mem_val["api_key"] == "[REDACTED]"
    assert mem_val["auth_token"] == "[REDACTED]"
    assert mem_val["db_password"] == "[REDACTED]"
    assert mem_val["accessToken"] == "[REDACTED]"
    assert mem_val["apiKey"] == "[REDACTED]"
    assert mem_val["clientSecret"] == "[REDACTED]"
    assert mem_val["notes"].count("[REDACTED]") == 3
    assert "free-form-api-key" not in mem_val["notes"]
    assert "free-form-bearer" not in mem_val["notes"]
    assert env.capability_summary.granted_secret_refs == ["ALLOWED_TOKEN", "registered_token"]


def test_persistence_sanitizer_rejects_oversized_envelope() -> None:
    """The durable boundary cannot be bypassed with oversized worker metadata."""
    env, status = assemble_context_envelope(
        task_id="t-oversized-persistence",
        session_id="s1",
        task_spec=TaskSpec(goal="Safe objective"),
        task_text="Safe objective",
        memory_context=None,
        worker_request=WorkerRequest(task_text="Safe objective"),
    )
    assert status == "assembled"
    assert env is not None
    metadata = env.model_dump(mode="json")
    metadata["objective"] = "x" * (MAX_ENVELOPE_BYTES + 1)

    with pytest.raises(ValueError, match="exceeds the persistence size limit"):
        sanitize_context_envelope_metadata(metadata)


def test_worktree_state_digest_git_failure_returns_none(tmp_path: Path) -> None:
    """Git failures (e.g. invalid git dir) return None and do not report clean worktree."""
    ws = tmp_path / "ws_invalid_git"
    ws.mkdir()
    (ws / "code.py").write_text("print(1)")
    invalid_git = tmp_path / "corrupt_git_dir"
    invalid_git.mkdir()
    # Directory exists but is not a valid git repository
    digest = _compute_worktree_state_digest(ws, trusted_git_dir=invalid_git)
    assert digest is None


def test_worktree_state_digest_untracked_large_files_no_collision(tmp_path: Path) -> None:
    """Large untracked files with identical prefixes but different suffixes

    produce distinct digests.
    """

    def _make_ws(name: str, suffix: bytes) -> Path:
        ws = tmp_path / name
        ws.mkdir()
        subprocess.run(["git", "init", str(ws)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(ws), "config", "user.email", "test@example.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(ws), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )
        (ws / "base.txt").write_text("base content")
        subprocess.run(["git", "-C", str(ws), "add", "base.txt"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(ws), "commit", "-m", "init"], check=True, capture_output=True
        )
        # Write large untracked file (>65536 bytes) with shared prefix
        large_prefix = b"X" * 70000
        (ws / "untracked_large.dat").write_bytes(large_prefix + suffix)
        return ws

    ws1 = _make_ws("ws1", b"_DIFFERING_SUFFIX_ONE")
    ws2 = _make_ws("ws2", b"_DIFFERING_SUFFIX_TWO")

    digest1 = _compute_worktree_state_digest(ws1)
    digest2 = _compute_worktree_state_digest(ws2)

    clean_digest = hashlib.sha256(b"clean").hexdigest()
    assert digest1 is not None and digest1 != clean_digest
    assert digest2 is not None and digest2 != clean_digest
    assert digest1 != digest2


def test_worktree_state_digest_binds_tracked_binary_contents(tmp_path: Path) -> None:
    """Same-size tracked binary mutations produce distinct worktree digests."""
    trusted_git, ws = _setup_git_worktree(tmp_path, "ws_binary")
    binary_path = ws / "asset.bin"
    binary_path.write_bytes(b"\x00" + (b"A" * 4096))
    subprocess.run(
        ["git", "--git-dir", str(trusted_git), "--work-tree", str(ws), "add", "asset.bin"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(trusted_git),
            "--work-tree",
            str(ws),
            "-c",
            "user.name=test",
            "-c",
            "user.email=t@e.com",
            "commit",
            "-m",
            "add binary",
        ],
        check=True,
        capture_output=True,
    )

    binary_path.write_bytes(b"\x00" + (b"B" * 4096))
    digest_b = _compute_worktree_state_digest(ws, trusted_git_dir=trusted_git)
    binary_path.write_bytes(b"\x00" + (b"C" * 4096))
    digest_c = _compute_worktree_state_digest(ws, trusted_git_dir=trusted_git)

    assert digest_b is not None
    assert digest_c is not None
    assert digest_b != digest_c


def test_worktree_state_digest_hashes_git_quoted_filenames(tmp_path: Path) -> None:
    """Untracked paths requiring Git quoting still bind their complete contents."""
    trusted_git, ws = _setup_git_worktree(tmp_path, "ws_quoted_path")
    unusual_path = ws / "line\nbreak.txt"
    unusual_path.write_text("version A", encoding="utf-8")

    digest_a = _compute_worktree_state_digest(ws, trusted_git_dir=trusted_git)
    unusual_path.write_text("version B", encoding="utf-8")
    digest_b = _compute_worktree_state_digest(ws, trusted_git_dir=trusted_git)

    assert digest_a is not None
    assert digest_b is not None
    assert digest_a != digest_b


def test_worktree_state_digest_git_timeout_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out Git command fails evidence assembly without blocking indefinitely."""
    trusted_git, ws = _setup_git_worktree(tmp_path, "ws_git_timeout")

    def _timeout(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=0.01)

    monkeypatch.setattr("orchestrator.context_envelope.subprocess.run", _timeout)

    assert _compute_worktree_state_digest(ws, trusted_git_dir=trusted_git) is None


def test_worktree_state_digest_status_failure_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed status command cannot be mistaken for a valid worktree digest."""
    trusted_git, ws = _setup_git_worktree(tmp_path, "ws_status_failure")

    real_run = subprocess.run

    def _dispatch(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "status" in cmd:
            kwargs["stderr"].write(b"status failed")
            return subprocess.CompletedProcess(cmd, 1)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("orchestrator.context_envelope.subprocess.run", _dispatch)

    assert _compute_worktree_state_digest(ws, trusted_git_dir=trusted_git) is None


def test_worktree_state_digest_handles_rename_records(tmp_path: Path) -> None:
    """NUL-delimited rename source records are consumed without path ambiguity."""
    trusted_git, ws = _setup_git_worktree(tmp_path, "ws_rename")
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(trusted_git),
            "--work-tree",
            str(ws),
            "mv",
            "app.py",
            "renamed.py",
        ],
        check=True,
        capture_output=True,
    )

    digest = _compute_worktree_state_digest(ws, trusted_git_dir=trusted_git)

    assert digest is not None
    assert digest != hashlib.sha256(b"clean").hexdigest()


def test_status_stream_and_file_hash_fail_closed(tmp_path: Path) -> None:
    """Malformed status records and unreadable paths fail evidence assembly closed."""
    ws = tmp_path / "ws_status_stream"
    ws.mkdir()
    hasher = hashlib.sha256()
    deadline = time.monotonic() + 1

    assert _hash_git_status_stream(io.BytesIO(b"?\0"), ws, hasher, deadline=deadline) is None
    assert (
        _hash_git_status_stream(io.BytesIO(b"R  renamed.py\0"), ws, hasher, deadline=deadline)
        is None
    )
    assert not _stream_hash_file(ws / "missing.txt", ws.resolve(), hasher)

    large_file = ws / "deadline.txt"
    large_file.write_bytes(b"x" * 70000)
    assert not _stream_hash_file(large_file, ws.resolve(), hasher, deadline=0)


def test_worktree_stream_helpers_bound_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git spooling and file hashing reject timeouts, unsafe paths, and I/O failures."""
    ws = tmp_path / "ws_stream_failures"
    ws.mkdir()
    git_dir = ws / ".git"
    git_dir.mkdir()

    def _successful_diff(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        kwargs["stdout"].write(b"diff payload")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("orchestrator.context_envelope.subprocess.run", _successful_diff)
    monkeypatch.setattr("orchestrator.context_envelope.time.monotonic", lambda: 2.0)
    assert _stream_git_diff(git_dir, ws, deadline=1.0) == (None, 0)

    def _io_failure(*args: Any, **kwargs: Any) -> None:
        raise OSError("git unavailable")

    monkeypatch.setattr("orchestrator.context_envelope.subprocess.run", _io_failure)
    assert _stream_git_diff(git_dir, ws, deadline=3.0) == (None, 0)

    def _status_timeout(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="git status", timeout=0.01)

    monkeypatch.setattr("orchestrator.context_envelope.subprocess.run", _status_timeout)
    assert _stream_git_status(git_dir, ws, hashlib.sha256(), deadline=3.0) is None

    target = ws / "target.txt"
    target.write_text("content", encoding="utf-8")
    symlink = ws / "target-link.txt"
    symlink.symlink_to(target)
    assert not _stream_hash_file(symlink, ws.resolve(), hashlib.sha256())

    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    assert not _stream_hash_file(outside, ws.resolve(), hashlib.sha256())

    with monkeypatch.context() as path_patch:
        path_patch.setattr(Path, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
        assert not _stream_hash_file(target, ws.resolve(), hashlib.sha256())


def test_status_stream_deadline_and_missing_untracked_file(tmp_path: Path) -> None:
    """Status parsing stops at its deadline and rejects missing untracked files."""
    ws = tmp_path / "ws_status_failures"
    ws.mkdir()

    assert (
        _hash_git_status_stream(io.BytesIO(b"?? pending.txt\0"), ws, hashlib.sha256(), deadline=0)
        is None
    )
    assert (
        _hash_git_status_stream(
            io.BytesIO(b"?? missing.txt\0"),
            ws,
            hashlib.sha256(),
            deadline=time.monotonic() + 1,
        )
        is None
    )
