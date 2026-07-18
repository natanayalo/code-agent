# ruff: noqa: F403, F405
"""Core orchestrator graph unit tests."""

from __future__ import annotations

from tests.unit.orchestrator_graph_unit_support import *  # noqa: F403


def test_ensure_state_from_dict():
    raw_dict = {"task": {"task_text": "do something"}}
    state = _ensure_state(raw_dict)
    assert isinstance(state, OrchestratorState)
    assert state.task.task_text == "do something"


def test_classify_task_kind():
    assert _classify_task_kind("hello") == "implementation"
    assert _classify_task_kind("refactor code") == "architecture"
    assert _classify_task_kind("investigate logs") == "ambiguous"


def test_is_destructive_task():
    assert is_destructive_task("test", {"destructive_action": True}) is True


def test_redact_effective_input_ignores_non_string_and_blank_secrets() -> None:
    result = _redact_effective_input(
        {"token": "visible", "message": "keep this value"},
        {"", "   ", 7},  # type: ignore[arg-type]
    )

    assert result == {"token": "[REDACTED]", "message": "keep this value"}


def test_task_requires_approval_ignores_untrusted_approved_status() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_id": "t1",
                "task_text": "Delete all files",
                "constraints": {"approval": {"status": "approved", "source": "user"}},
            }
        }
    )
    assert _task_requires_approval(state) is True


def test_task_requires_approval_accepts_trusted_approved_status() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_id": "t1",
                "task_text": "Delete all files",
                "constraints": {"approval": {"status": "approved", "source": "api"}},
            }
        }
    )
    assert _task_requires_approval(state) is False


def test_coerce_approval_decision():
    assert _coerce_approval_decision(True) is True
    assert _coerce_approval_decision({"approved": True}) is True
    assert _coerce_approval_decision({"approved": False}) is False
    assert _coerce_approval_decision({"approved": "y"}) is True
    assert _coerce_approval_decision({"approved": "no"}) is False
    assert _coerce_approval_decision({"approved": 123}) is False
    assert _coerce_approval_decision({"other": "field"}) is False
    assert _coerce_approval_decision("yes") is True
    assert _coerce_approval_decision("no") is False


def test_default_worker_result_provider():
    request = WorkerRequest(task_text="demo")
    res = _default_worker_result_provider(request)
    assert res.status == "success"


def test_build_worker_request_from_state():
    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "session-1",
                "user_id": "user-1",
                "channel": "telegram",
                "external_thread_id": "thread-1",
            },
            "task": {
                "task_text": "Add worker interface",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "task/t-040-worker-interface",
                "constraints": {"requires_approval": False},
                "budget": {"max_minutes": 15},
            },
            "task_spec": {
                "goal": "Add worker interface",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "target_branch": "task/t-040-worker-interface",
                "risk_level": "medium",
                "task_type": "feature",
                "delivery_mode": "workspace",
            },
            "task_plan": {
                "triggered": True,
                "complexity_reason": "architecture",
                "steps": [
                    {
                        "step_id": "1",
                        "title": "Inspect",
                        "expected_outcome": "Find target files",
                    }
                ],
            },
            "route": {
                "chosen_worker": "codex",
                "chosen_profile": "codex-native-executor",
                "runtime_mode": "native_agent",
            },
        }
    )
    request = _build_worker_request(state)
    assert request.session_id == "session-1"
    assert request.repo_url == "https://github.com/natanayalo/code-agent"
    assert request.branch == "task/t-040-worker-interface"
    assert request.task_text == "Add worker interface"
    assert request.task_plan is not None
    assert request.task_plan["complexity_reason"] == "architecture"
    assert request.task_spec is not None
    assert request.task_spec["goal"] == "Add worker interface"
    assert request.constraints == {"requires_approval": False}
    assert request.budget == {"max_minutes": 15}
    assert request.worker_profile == "codex-native-executor"
    assert request.runtime_mode == "native_agent"
    assert request.runtime_manifest is not None
    assert request.runtime_manifest["worker"]["worker_type"] == "codex"
    assert request.runtime_manifest["worker"]["worker_profile"] == "codex-native-executor"
    assert request.runtime_manifest["worker"]["runtime_mode"] == "native_agent"
    assert request.runtime_manifest["task"]["delivery_mode"] == "workspace"
    assert request.runtime_manifest["task"]["budget"] == {"max_minutes": 15}
    assert request.runtime_manifest["maintenance_actions"][0]["request_only"] is True


def test_build_worker_request_tolerates_missing_route() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Run a task"},
            "dispatch": {
                "worker_type": "codex",
                "worker_profile": "codex-native-executor",
                "runtime_mode": "native_agent",
            },
        }
    )
    state.route = None  # type: ignore[assignment]

    request = _build_worker_request(state)

    assert request.worker_profile == "codex-native-executor"
    assert request.runtime_mode == "native_agent"
    assert request.runtime_manifest is not None
    assert request.runtime_manifest["worker"]["worker_type"] == "codex"
    assert request.runtime_manifest["worker"]["worker_profile"] == "codex-native-executor"


def test_build_worker_request_tolerates_missing_dispatch_and_constraints() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Run a task"},
            "route": {
                "chosen_worker": "codex",
                "chosen_profile": "codex-native-executor",
                "runtime_mode": "native_agent",
            },
        }
    )
    state.dispatch = None  # type: ignore[assignment]
    state.task.constraints = None  # type: ignore[assignment]

    request = _build_worker_request(state)

    assert request.worker_profile == "codex-native-executor"
    assert request.runtime_mode == "native_agent"
    assert request.constraints == {}
    assert request.workspace_id is None
    assert request.runtime_manifest is not None
    assert request.runtime_manifest["worker"]["worker_type"] == "codex"
    assert request.runtime_manifest["worker"]["worker_profile"] == "codex-native-executor"
    assert request.runtime_manifest["worker"]["workspace_id"] is None


def test_build_worker_request_uses_json_schema_for_scout_tasks() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Scout the repo",
                "constraints": {"max_proposals": 2},
            },
            "task_spec": {
                "goal": "Scout the repo",
                "task_type": "scout",
                "delivery_mode": "summary",
            },
            "route": {
                "chosen_worker": "codex",
                "chosen_profile": "codex-native-executor",
                "runtime_mode": "native_agent",
            },
        }
    )

    request = _build_worker_request(state)

    assert request.response_format == "json"
    assert request.response_schema is not None
    assert request.response_schema["properties"]["proposals"]["maxItems"] == 2
    assert request.response_schema["$defs"]["ScoutProposal"]["additionalProperties"] is False


def test_build_worker_request_does_not_infer_read_only_from_profile_name() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Smoke test: print PWD and HOME only, then exit.",
                "constraints": {"requires_approval": False},
            },
            "route": {
                "chosen_worker": "codex",
                "chosen_profile": "codex-native-executor-read-only",
                "runtime_mode": "native_agent",
            },
        }
    )

    request = _build_worker_request(state)

    assert request.worker_profile == "codex-native-executor-read-only"
    assert request.constraints == {"requires_approval": False}


def test_build_worker_request_prefers_review_repair_handoff_text():
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Original task",
                "constraints": {"independent_review_repair_request": "Repair follow-up task"},
            },
            "normalized_task_text": "Normalized original task",
        }
    )

    request = _build_worker_request(state)

    assert request.task_text == "Repair follow-up task"


def test_build_worker_request_combines_verifier_and_review_repair_handoff_text():
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Original task",
                "constraints": {
                    "independent_verifier_repair_request": "Verifier repair follow-up task",
                    "independent_review_repair_request": "Review repair follow-up task",
                },
            },
            "normalized_task_text": "Normalized original task",
        }
    )

    request = _build_worker_request(state)

    assert request.task_text.startswith("Apply the following repair instructions in one pass.")
    assert "Verifier repair instructions:" in request.task_text
    assert "Verifier repair follow-up task" in request.task_text
    assert "Independent review repair instructions:" in request.task_text
    assert "Review repair follow-up task" in request.task_text


def test_plan_task_skips_simple_tasks():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "Add a helper"}, "task_kind": "implementation"}
    )

    res = plan_task(state)

    assert res["current_step"] == "plan_task"
    assert res["task_plan"] is None
    assert res["progress_updates"][-1] == "planning skipped: task is straightforward"


def test_plan_task_generates_plan_for_complex_tasks():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Refactor architecture across files"},
            "task_kind": "architecture",
            "normalized_task_text": "Refactor architecture across files",
        }
    )

    res = plan_task(state)

    assert res["current_step"] == "plan_task"
    assert res["task_plan"]["triggered"] is True
    assert res["task_plan"]["complexity_reason"] == "architectural_task"
    assert len(res["task_plan"]["steps"]) == 3
    assert res["progress_updates"][-1] == "structured plan generated (architectural_task)"


def test_plan_task_parameterizes_steps_for_ambiguous_tasks():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Investigate flaky behavior in worker runs"},
            "task_kind": "ambiguous",
        }
    )

    res = plan_task(state)

    assert res["task_plan"]["complexity_reason"] == "ambiguous_task"
    assert res["task_plan"]["steps"][0]["title"] == "Investigate Root Cause and Scope"


def test_plan_task_parameterizes_multi_file_execution_step():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Implement change across files in orchestrator"},
            "task_kind": "implementation",
        }
    )

    res = plan_task(state)

    assert res["task_plan"]["complexity_reason"] == "multi_file_task"
    assert res["task_plan"]["steps"][1]["title"] == "Sequence Multi-file Changes Safely"


def test_plan_task_detects_multifile_compound_marker():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Apply multifile change across orchestrator modules"},
            "task_kind": "implementation",
        }
    )

    res = plan_task(state)

    assert res["task_plan"]["complexity_reason"] == "multi_file_task"


@pytest.mark.asyncio
async def test_generate_task_spec_creates_policy_checked_contract_before_routing() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Delete all generated files",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "master",
            },
            "task_kind": "implementation",
        }
    )

    res = await generate_task_spec(state)

    assert res["current_step"] == "generate_task_spec"
    assert res["task_spec"]["goal"] == "Delete all generated files"
    assert res["task_spec"]["requires_permission"] is True
    assert res["task_spec"]["risk_level"] == "high"
    assert res["timeline_events"][0].event_type == "task_spec_generated"
    assert res["timeline_events"][0].payload["policy_violations"] == []


@pytest.mark.anyio
async def test_generate_task_spec_applies_loaded_repo_profile_after_generation() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Update db/migrations/001_init.py",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "master",
            },
            "task_kind": "implementation",
            "repo_profile": {
                "setup": {"commands": ["npm ci"]},
                "validation": {
                    "quick": ["npm test -- --runInBand"],
                    "full": ["npm run test:coverage"],
                },
                "protected_paths": ["db/migrations"],
                "delivery": {"default_mode": "branch"},
            },
        }
    )

    res = await generate_task_spec(state)

    assert res["task_spec"]["setup_commands"] == ["npm ci"]
    assert res["task_spec"]["risk_level"] == "high"
    assert res["task_spec"]["requires_permission"] is True
    assert res["task_spec"]["permission_reason"] == "Task may affect protected paths"
    assert res["task_spec"]["verification_commands"] == ["npm run test:coverage"]
    assert res["task_spec"]["delivery_mode"] == "branch"


@pytest.mark.asyncio
async def test_generate_task_spec_applies_brain_enrichment_with_policy_clamps() -> None:
    class _FakeBrain:
        async def suggest_task_spec(self, **kwargs):
            del kwargs
            return TaskSpecBrainSuggestion(
                acceptance_criteria=["Document verifier pass/fail details in the summary."],
                verification_commands=["pytest tests/unit/test_orchestrator_graph_unit.py -q"],
                suggested_risk_level="high",
                suggested_task_type="docs",
                rationale="Increase scrutiny for risky workflow change.",
            )

    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Investigate flaky verifier behavior",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "main",
            },
            "task_kind": "implementation",
        }
    )

    res = await generate_task_spec(state, orchestrator_brain=_FakeBrain())

    assert res["task_spec"]["risk_level"] == "high"
    assert res["task_spec"]["requires_permission"] is True
    assert res["task_spec"]["task_type"] == "investigation"
    assert res["task_spec"]["verification_commands"] == [
        "pytest tests/unit/test_orchestrator_graph_unit.py -q"
    ]
    assert "task spec generated with brain enrichment" in res["progress_updates"]
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["provider"] == "_FakeBrain"
    assert brain_payload["applied"] is True
    assert brain_payload["ignored_fields"] == ["suggested_task_type"]
    assert brain_payload["added_acceptance_criteria"] == [
        "Document verifier pass/fail details in the summary."
    ]
    assert brain_payload["added_verification_commands"] == [
        "pytest tests/unit/test_orchestrator_graph_unit.py -q"
    ]


@pytest.mark.asyncio
async def test_generate_task_spec_brain_failures_fall_back_to_deterministic_spec() -> None:
    class _ExplodingBrain:
        async def suggest_task_spec(self, **kwargs):
            del kwargs
            raise RuntimeError("brain unavailable")

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Add API pagination"},
            "task_kind": "implementation",
        }
    )

    res = await generate_task_spec(state, orchestrator_brain=_ExplodingBrain())

    assert res["task_spec"]["goal"] == "Add API pagination"
    brain_payload = res["timeline_events"][0].payload["brain"]
    assert brain_payload["provider"] == "_ExplodingBrain"
    assert "RuntimeError: brain unavailable" == brain_payload["error"]


@pytest.mark.asyncio
async def test_generate_task_spec_brain_fallback_emits_normalized_attributes() -> None:
    class _ExplodingBrain:
        async def suggest_task_spec(self, **kwargs):
            del kwargs
            raise RuntimeError("brain unavailable")

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Add API pagination"},
            "task_kind": "implementation",
        }
    )
    with patch("orchestrator.graph.set_current_span_attribute") as mock_set_attr:
        await generate_task_spec(state, orchestrator_brain=_ExplodingBrain())

    attrs = {call.args[0]: call.args[1] for call in mock_set_attr.call_args_list}
    assert attrs["code_agent.fallback.used"] is True
    assert attrs["code_agent.fallback.from"] == "_ExplodingBrain"
    assert attrs["code_agent.fallback.to"] == "deterministic_task_spec"
    assert attrs["code_agent.fallback.reason_code"] == "brain_error"


@pytest.mark.asyncio
async def test_generate_task_spec_and_route_node_applies_unified_brain_route() -> None:
    class _Brain:
        async def suggest_task_spec_and_route(self, **kwargs):
            del kwargs
            return UnifiedOrchestratorSuggestion(
                assumptions=[],
                acceptance_criteria=[],
                non_goals=[],
                clarification_questions=[],
                verification_commands=[],
                suggested_worker="antigravity",
                suggested_profile="antigravity-native-executor-read-only",
                suggested_retry_strategy=None,
                rationale="u",
            )

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "print pwd/home"},
            "task_kind": "implementation",
        }
    )
    profiles = {
        "antigravity-native-executor-read-only": WorkerProfile(
            name="antigravity-native-executor-read-only",
            worker_type="antigravity",
            runtime_mode="native_agent",
            mutation_policy="read_only",
            capability_tags=["execution"],
        )
    }
    node = build_generate_task_spec_and_route_node(
        _ALL_WORKERS,
        available_profiles=profiles,
        orchestrator_brain=_Brain(),
    )
    res = await node(state)

    assert res["current_step"] == "generate_task_spec_and_route"
    assert res["route"]["chosen_worker"] == "antigravity"
    assert res["route"]["chosen_profile"] == "antigravity-native-executor-read-only"


@pytest.mark.anyio
async def test_generate_task_spec_and_route_node_applies_repo_profile_before_route() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Implement the release note",
                "worker_override": "codex",
            },
            "task_kind": "implementation",
            "repo_profile": {
                "delivery": {"default_mode": "draft_pr"},
            },
        }
    )
    profiles = {
        "codex-workspace": WorkerProfile(
            name="codex-workspace",
            worker_type="codex",
            runtime_mode="native_agent",
            capability_tags=["execution"],
            supported_delivery_modes=["workspace"],
        ),
        "codex-draft-pr": WorkerProfile(
            name="codex-draft-pr",
            worker_type="codex",
            runtime_mode="native_agent",
            capability_tags=["execution"],
            supported_delivery_modes=["draft_pr"],
        ),
    }
    node = build_generate_task_spec_and_route_node(
        frozenset({"codex"}),
        available_profiles=profiles,
    )

    res = await node(state)

    assert res["task_spec"]["delivery_mode"] == "draft_pr"
    assert res["route"]["chosen_worker"] == "codex"
    assert res["route"]["chosen_profile"] == "codex-draft-pr"


@pytest.mark.asyncio
async def test_generate_task_spec_and_route_node_fails_when_unified_method_missing() -> None:
    class _LegacyOnlyBrain:
        async def suggest_task_spec(self, **kwargs):
            del kwargs
            return None

        async def suggest_route(self, **kwargs):
            del kwargs
            return None

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "print pwd/home"},
            "task_kind": "implementation",
        }
    )
    node = build_generate_task_spec_and_route_node(
        _ALL_WORKERS,
        orchestrator_brain=_LegacyOnlyBrain(),
    )
    res = await node(state)

    assert res["current_step"] == "generate_task_spec_and_route"
    assert res["result"].status == "error"
    assert "brain_unified_method_missing" in res["errors"]
    assert res["timeline_events"][0].event_type == "task_spec_and_route_generated"
    assert res["timeline_events"][0].payload["error_reason_code"] == "brain_unified_method_missing"


def test_plan_task_complexity_marker_uses_word_boundaries():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Rename the multi-file-uploader module"},
            "task_kind": "implementation",
        }
    )

    res = plan_task(state)

    assert res["task_plan"] is None
    assert res["progress_updates"][-1] == "planning skipped: task is straightforward"


def test_resolve_orchestrator_timeout_seconds_prefers_explicit_override() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Run a worker",
                "budget": {
                    "orchestrator_timeout_seconds": "45",
                    "worker_timeout_seconds": 12,
                    "max_minutes": 9,
                },
            }
        }
    )

    assert _resolve_orchestrator_timeout_seconds(state) == 45


def test_resolve_orchestrator_timeout_seconds_accepts_float_like_strings() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Run a worker",
                "budget": {"orchestrator_timeout_seconds": "45.0"},
            }
        }
    )

    assert _resolve_orchestrator_timeout_seconds(state) == 45


def test_resolve_orchestrator_timeout_seconds_falls_back_to_worker_budget() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Run a worker",
                "budget": {"worker_timeout_seconds": 12},
            }
        }
    )

    assert _resolve_orchestrator_timeout_seconds(state) == 42


def test_resolve_orchestrator_timeout_seconds_falls_back_to_max_minutes() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Run a worker",
                "budget": {"max_minutes": 2},
            }
        }
    )

    assert _resolve_orchestrator_timeout_seconds(state) == 150


def test_summarize_result_no_result():
    state = OrchestratorState.model_validate({"task": {"task_text": "demo"}})
    res = summarize_result(state)
    assert res["result"]["status"] == "error"


def test_summarize_result_uses_normalized_task_text_for_active_goal():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "  demo  "},
            "normalized_task_text": "demo",
            "result": {
                "status": "success",
                "summary": "done",
                "commands_run": [],
                "files_changed": ["demo.txt"],
                "test_results": [],
                "artifacts": [],
            },
        }
    )

    res = summarize_result(state)

    assert res["session_state_update"]["active_goal"] == "demo"
    assert res["session_state_update"]["files_touched"] == ["demo.txt"]


def test_summarize_result_attaches_task_plan_artifact_when_present():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "dispatch": {"worker_type": "codex"},
            "task_plan": {
                "triggered": True,
                "complexity_reason": "ambiguous_task",
                "steps": [
                    {
                        "step_id": "1",
                        "title": "Inspect",
                        "expected_outcome": "Find root cause",
                    }
                ],
            },
            "result": {
                "status": "success",
                "summary": "done",
                "commands_run": [],
                "files_changed": ["demo.txt"],
                "test_results": [],
                "artifacts": [],
            },
        }
    )

    res = summarize_result(state)

    artifact = res["result"]["artifacts"][0]
    assert artifact["name"] == "task_plan"
    assert artifact["artifact_type"] == "result_summary"
    assert artifact["uri"].startswith("task-plan://sha256/")


def test_summarize_result_reviewer_findings_without_leading_newline_when_summary_empty():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "summary": "",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
            "review": {
                "reviewer_kind": "independent_reviewer",
                "summary": "Advisory findings detected.",
                "confidence": 0.8,
                "outcome": "findings",
                "findings": [
                    {
                        "severity": "medium",
                        "category": "correctness",
                        "confidence": 0.7,
                        "file_path": "orchestrator/review.py",
                        "line_start": 1,
                        "line_end": 1,
                        "title": "Example finding",
                        "why_it_matters": "Example impact.",
                    }
                ],
            },
        }
    )

    res = summarize_result(state)
    summary = res["result"]["summary"]

    assert summary.startswith("---\n### Reviewer Findings")
    assert not summary.startswith("\n")


def test_create_in_memory_checkpointer():
    cp = create_in_memory_checkpointer()
    assert cp is not None
