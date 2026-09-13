import json
from pathlib import Path

from sandbox.redact import SecretRedactor
from workers.agent_event import (
    AgentCompleted,
    AgentFailed,
    AgentProgress,
    AgentStarted,
    FileChanged,
    assert_event_sequence_monotonic,
)
from workers.agent_event_normalizer import (
    NormalizationConfig,
    normalize_provider_stream,
    safe_truncate_text,
)
from workers.base import ArtifactReference
from workers.codex_event_normalizer import CodexStreamNormalizer
from workers.native_agent_models import NativeAgentRunRequest
from workers.native_agent_runner import (
    _build_timeout_events,
    _persist_normalized_events_artifact,
    _process_native_agent_events,
    _resolve_run_id,
)


def test_safe_truncate_text_none_and_empty():
    assert safe_truncate_text(None) is None
    assert safe_truncate_text("") == ""


def test_safe_truncate_text_redaction_and_length():
    redactor = SecretRedactor({"MY_SECRET_KEY"})
    text = "The key is MY_SECRET_KEY, keep it secret!" * 50
    truncated = safe_truncate_text(text, redactor=redactor, limit=100)
    assert truncated is not None
    assert len(truncated) <= 100
    assert "MY_SECRET_KEY" not in truncated
    assert "[REDACTED]" in truncated


def test_safe_truncate_text_dict_serialization():
    payload = {"key": "value", "num": 42}
    result = safe_truncate_text(payload, limit=200)
    assert result is not None
    assert '"key": "value"' in result


def test_normalize_provider_stream_lifecycle_injection():
    normalizer = CodexStreamNormalizer()
    raw_records = [
        json.dumps({"type": "message_delta", "delta": "Analyzing..."}),
    ]

    events, stats = normalize_provider_stream(
        raw_records,
        normalizer,
        run_id="run_test",
        default_exit_code=0,
        files_changed=["foo.py", "bar.py"],
    )

    assert stats.normalized == 1
    assert stats.total_raw == 1
    assert len(events) == 5  # Started + Progress + 2 FileChanged + Completed
    assert isinstance(events[0], AgentStarted)
    assert isinstance(events[1], AgentProgress)
    assert isinstance(events[2], FileChanged)
    assert isinstance(events[3], FileChanged)
    assert isinstance(events[4], AgentCompleted)
    assert_event_sequence_monotonic(events, strictly_consecutive=True)


def test_normalize_provider_stream_failed_exit_code_injection():
    normalizer = CodexStreamNormalizer()
    raw_records = [
        {"type": "message_delta", "delta": "Step 1"},
    ]

    events, stats = normalize_provider_stream(
        raw_records,
        normalizer,
        run_id="run_fail",
        default_exit_code=1,
    )

    assert isinstance(events[-1], AgentFailed)
    assert events[-1].exit_code == 1
    assert "Process exited with code 1" in events[-1].failure_summary
    assert_event_sequence_monotonic(events, strictly_consecutive=True)


def test_normalize_provider_stream_cap_and_overflow():
    normalizer = CodexStreamNormalizer()
    # 15 message deltas with max_events_per_run=10 and reserved=2 -> limit is 8 non-lifecycle
    raw_records = [{"type": "message_delta", "delta": f"step {i}"} for i in range(15)]

    config = NormalizationConfig(max_events_per_run=10, reserved_lifecycle_events=2)
    events, stats = normalize_provider_stream(
        raw_records,
        normalizer,
        config=config,
        run_id="run_overflow",
        default_exit_code=0,
    )

    assert stats.total_raw == 15
    assert stats.normalized == 8
    assert stats.dropped_overflow == 7
    assert_event_sequence_monotonic(events, strictly_consecutive=True)


def test_normalize_provider_stream_malformed_and_unknown():
    normalizer = CodexStreamNormalizer()
    raw_records = [
        "not json at all",
        "",  # blank string ignored
        json.dumps(["not a dict"]),
        json.dumps({"no_type_field": 123}),
        json.dumps({"type": "unknown_future_codex_event"}),
        json.dumps({"type": "message_delta", "delta": "valid message"}),
    ]

    events, stats = normalize_provider_stream(
        raw_records,
        normalizer,
        run_id="run_dirty",
        default_exit_code=0,
    )

    assert stats.total_raw == 5  # 6 records minus 1 empty line
    assert stats.dropped_malformed == 3  # non-json, list, no type field
    assert stats.dropped_unknown == 1  # unknown_future_codex_event
    assert stats.normalized == 1  # valid message_delta
    assert_event_sequence_monotonic(events, strictly_consecutive=True)


def test_safe_truncate_text_redact_false_and_truncation():
    result = safe_truncate_text("abcdefghijklmnop", redact=False, limit=5)
    assert result == "abcde"


def test_normalize_provider_stream_non_dict_non_str_record():
    normalizer = CodexStreamNormalizer()
    events, stats = normalize_provider_stream(
        [12345],  # type: ignore[list-item]
        normalizer,
        run_id="run_non_dict",
        default_exit_code=0,
    )
    assert stats.dropped_malformed == 1
    assert stats.total_raw == 1


def test_normalize_provider_stream_existing_started_and_completed():
    normalizer = CodexStreamNormalizer()
    raw_records = [
        {"type": "session_created", "command": "codex exec"},
        {"type": "file_edit", "path": "test.py", "change_kind": "modified"},
        {"type": "session_complete", "summary": "done early"},
    ]
    events, stats = normalize_provider_stream(
        raw_records,
        normalizer,
        run_id="run_existing",
        default_exit_code=0,
        files_changed=["test.py"],  # already in events, should not duplicate
    )
    assert isinstance(events[0], AgentStarted)
    assert isinstance(events[-1], AgentCompleted)
    file_events = [e for e in events if isinstance(e, FileChanged)]
    assert len(file_events) == 1
    assert_event_sequence_monotonic(events, strictly_consecutive=True)


def test_normalize_provider_stream_normalizer_rejects_event():
    normalizer = CodexStreamNormalizer()
    # file_edit without path is a known type to Codex normalizer, but normalizer returns None
    raw_records = [{"type": "file_edit"}]
    events, stats = normalize_provider_stream(
        raw_records,
        normalizer,
        run_id="run_reject",
        default_exit_code=0,
    )
    assert stats.dropped_malformed == 1
    assert stats.normalized == 0


def test_normalize_provider_stream_lifecycle_overflow():
    normalizer = CodexStreamNormalizer()
    config = NormalizationConfig(max_events_per_run=2, reserved_lifecycle_events=0)
    raw_records = [
        {"type": "message_delta", "delta": "one"},
        {"type": "message_delta", "delta": "two"},
        {"type": "session_complete", "summary": "done"},
    ]
    events, stats = normalize_provider_stream(
        raw_records,
        normalizer,
        config=config,
        run_id="run_lifecycle_overflow",
        default_exit_code=0,
    )
    assert stats.dropped_overflow >= 1


def test_process_native_agent_events_malformed_records_no_divergence(tmp_path: Path):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    artifacts: list[ArtifactReference] = []

    request = NativeAgentRunRequest(
        command=["codex"],
        prompt="do stuff",
        repo_path=tmp_path / "repo",
        workspace_path=tmp_path / "ws",
        normalizer=CodexStreamNormalizer(),
        worker_type="codex",
    )

    events, stats = _process_native_agent_events(
        request,
        artifact_root=artifact_root,
        events_path=None,
        stdout_text="invalid json line 1\ninvalid json line 2\n",
        files_changed=[],
        exit_code=0,
        artifacts=artifacts,
    )

    assert stats is not None
    assert stats.normalized == 0
    assert stats.dropped_malformed == 2
    assert events == []
    assert len(artifacts) == 0
    assert not (artifact_root / "agent-events-v1.jsonl").exists()


def test_process_native_agent_events_valid_records_writes_artifact(tmp_path: Path):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    artifacts: list[ArtifactReference] = []

    request = NativeAgentRunRequest(
        command=["codex"],
        prompt="do stuff",
        repo_path=tmp_path / "repo",
        workspace_path=tmp_path / "ws",
        normalizer=CodexStreamNormalizer(),
        worker_type="codex",
    )

    events, stats = _process_native_agent_events(
        request,
        artifact_root=artifact_root,
        events_path=None,
        stdout_text=json.dumps({"type": "message_delta", "delta": "Working..."}),
        files_changed=[],
        exit_code=0,
        artifacts=artifacts,
    )

    assert stats is not None
    assert stats.normalized == 1
    assert len(events) >= 2
    assert len(artifacts) == 1
    assert artifacts[0].name == "agent_event_stream"
    assert (artifact_root / "agent-events-v1.jsonl").is_file()


def test_process_native_agent_events_prefers_events_path_file_if_present(tmp_path: Path):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        json.dumps({"type": "message_delta", "delta": "From file"}), encoding="utf-8"
    )
    artifacts: list[ArtifactReference] = []

    request = NativeAgentRunRequest(
        command=["codex"],
        prompt="do stuff",
        repo_path=tmp_path / "repo",
        workspace_path=tmp_path / "ws",
        normalizer=CodexStreamNormalizer(),
        worker_type="codex",
    )

    events, stats = _process_native_agent_events(
        request,
        artifact_root=artifact_root,
        events_path=events_file,
        stdout_text=json.dumps({"type": "message_delta", "delta": "From stdout"}),
        files_changed=[],
        exit_code=0,
        artifacts=artifacts,
    )

    assert stats is not None
    assert stats.normalized == 1
    progress_event = next(e for e in events if isinstance(e, AgentProgress))
    assert progress_event.message == "From file"


def test_process_native_agent_events_absent_events_path_logs_warning_and_returns_empty(
    tmp_path: Path, caplog
):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    non_existent_events_path = tmp_path / "non_existent_events.jsonl"
    artifacts: list[ArtifactReference] = []

    request = NativeAgentRunRequest(
        command=["codex"],
        prompt="do stuff",
        repo_path=tmp_path / "repo",
        workspace_path=tmp_path / "ws",
        normalizer=CodexStreamNormalizer(),
        worker_type="codex",
    )

    with caplog.at_level("WARNING"):
        events, stats = _process_native_agent_events(
            request,
            artifact_root=artifact_root,
            events_path=non_existent_events_path,
            stdout_text="some stdout prose that should never be read as events",
            files_changed=[],
            exit_code=0,
            artifacts=artifacts,
        )

    assert events == []
    assert stats is None
    assert len(artifacts) == 0
    assert not (artifact_root / "agent-events-v1.jsonl").exists()
    assert any(
        "Expected provider event stream not found or empty" in r.message for r in caplog.records
    )


def test_normalize_provider_stream_reconciles_success_event_with_failed_exit_code():
    normalizer = CodexStreamNormalizer()
    raw_records = [
        {"type": "message_delta", "delta": "Doing work"},
        {"type": "session_complete", "summary": "Done early", "exit_code": 0},
    ]

    events, stats = normalize_provider_stream(
        raw_records,
        normalizer,
        run_id="run_reconcile",
        default_exit_code=2,
    )

    terminals = [e for e in events if isinstance(e, AgentCompleted | AgentFailed)]
    assert len(terminals) == 1
    assert isinstance(terminals[0], AgentFailed)
    assert terminals[0].exit_code == 2
    assert_event_sequence_monotonic(events, strictly_consecutive=True)


def test_normalize_provider_stream_strips_multiple_intermediate_terminals():
    normalizer = CodexStreamNormalizer()
    raw_records = [
        {"type": "session_complete", "summary": "Turn 1 complete"},
        {"type": "message_delta", "delta": "Continuing to turn 2"},
        {"type": "session_complete", "summary": "Turn 2 complete"},
    ]

    events, stats = normalize_provider_stream(
        raw_records,
        normalizer,
        run_id="run_multi_term",
        default_exit_code=0,
    )

    terminals = [e for e in events if isinstance(e, AgentCompleted | AgentFailed)]
    assert len(terminals) == 1
    assert isinstance(terminals[0], AgentCompleted)
    assert terminals[0].final_summary == "Turn 2 complete"
    assert_event_sequence_monotonic(events, strictly_consecutive=True)


def test_process_native_agent_events_run_id_decoupled_from_task_id(tmp_path: Path):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    artifacts: list[ArtifactReference] = []

    # 1. Explicit run_id provided
    req_explicit = NativeAgentRunRequest(
        command=["codex"],
        prompt="test",
        repo_path=tmp_path / "repo",
        workspace_path=tmp_path / "ws",
        normalizer=CodexStreamNormalizer(),
        task_id="task-xyz",
        run_id="run-custom-123",
    )
    events, _ = _process_native_agent_events(
        req_explicit,
        artifact_root=artifact_root,
        events_path=None,
        stdout_text=json.dumps({"type": "message_delta", "delta": "step"}),
        files_changed=[],
        exit_code=0,
        artifacts=artifacts,
    )
    assert len(events) > 0
    assert all(e.run_id == "run-custom-123" for e in events)
    assert all(e.task_id == "task-xyz" for e in events)

    # 2. Generated unique run_id when not provided (must not equal task_id)
    artifacts_gen: list[ArtifactReference] = []
    req_gen = NativeAgentRunRequest(
        command=["codex"],
        prompt="test",
        repo_path=tmp_path / "repo",
        workspace_path=tmp_path / "ws",
        normalizer=CodexStreamNormalizer(),
        task_id="task-xyz",
        run_id=None,
    )
    events_gen, _ = _process_native_agent_events(
        req_gen,
        artifact_root=artifact_root,
        events_path=None,
        stdout_text=json.dumps({"type": "message_delta", "delta": "step"}),
        files_changed=[],
        exit_code=0,
        artifacts=artifacts_gen,
    )
    assert len(events_gen) > 0
    assert events_gen[0].run_id.startswith("run-")
    assert events_gen[0].run_id != "task-xyz"
    assert all(e.task_id == "task-xyz" for e in events_gen)


def test_persist_normalized_events_artifact_writes_jsonl_and_attaches_artifact(tmp_path: Path):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    artifacts: list[ArtifactReference] = []

    events = [
        AgentStarted(run_id="run-1", sequence=1, task_id="task-1"),
        AgentCompleted(
            run_id="run-1", sequence=2, task_id="task-1", final_summary="Done", exit_code=0
        ),
    ]

    _persist_normalized_events_artifact(events, artifact_root, artifacts)

    art = next((a for a in artifacts if a.name == "agent_event_stream"), None)
    assert art is not None
    assert art.artifact_type == "agent_event_stream"

    art_file = Path(art.uri.removeprefix("file://"))
    assert art_file.is_file()
    assert art_file.name == "agent-events-v1.jsonl"
    lines = art_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_build_timeout_events_uses_resolved_run_id():
    req = NativeAgentRunRequest(
        command=["codex"],
        prompt="test",
        repo_path=Path("/tmp/repo"),
        workspace_path=Path("/tmp/ws"),
        normalizer=CodexStreamNormalizer(),
        task_id="task-timeout-test",
        run_id="run-timeout-fixed",
    )
    events = _build_timeout_events(req, "Timed out after 10s")
    assert _resolve_run_id(req) == "run-timeout-fixed"
    assert len(events) == 2
    assert events[0].run_id == "run-timeout-fixed"
    assert events[0].task_id == "task-timeout-test"
    assert events[1].run_id == "run-timeout-fixed"
    assert events[1].failure_kind == "timeout"
