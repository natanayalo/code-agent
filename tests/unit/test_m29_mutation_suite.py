"""Unit tests for the frozen M29 feature/mutation suite and evaluator contract."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from evaluation.m29_mutation_suite import (
    DEFAULT_FIXTURE_REVISION,
    DEFAULT_M29_NEUTRAL_EVALUATOR,
    M29_MUTATION_PROMPT_TRAILER,
    M29_MUTATION_SUITE_NAME,
    RAW_PAIR_DEFINITIONS,
    EvaluatorBudget,
    M29MutationCase,
    M29MutationSuite,
    MutationTaskBudget,
    NeutralEvaluatorManifest,
    build_m29_mutation_suite,
    load_frozen_mutation_suite,
)


def test_frozen_suite_loads_and_passes_validation() -> None:
    suite = load_frozen_mutation_suite()
    assert suite.suite_name == M29_MUTATION_SUITE_NAME
    assert suite.schema_version == 1
    assert suite.target_repository_revision == DEFAULT_FIXTURE_REVISION
    assert len(suite.cases) == 20
    assert len(suite.case_ids_by_profile()["codex-native-executor"]) == 10
    assert len(suite.case_ids_by_profile()["antigravity-native-executor"]) == 10
    assert len(suite.cases_by_profile()["codex-native-executor"]) == 10
    assert len(suite.cases_by_profile()["antigravity-native-executor"]) == 10

    pairs = suite.pair_cases()
    assert len(pairs) == 10
    for c1, c2 in pairs:
        assert c1.pair_order == 1
        assert c2.pair_order == 2
        assert c1.pair_group == c2.pair_group
        assert c1.topic == c2.topic
        assert c1.prompt == c2.prompt

    digest = suite.compute_digest()
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_suite_counterbalancing_alternation() -> None:
    suite = build_m29_mutation_suite()
    pairs = suite.pair_cases()
    for idx, (c1, c2) in enumerate(pairs, start=1):
        if idx % 2 == 1:
            assert c1.worker_profile == "codex-native-executor"
            assert c2.worker_profile == "antigravity-native-executor"
        else:
            assert c1.worker_profile == "antigravity-native-executor"
            assert c2.worker_profile == "codex-native-executor"


def test_suite_prompt_trailer_presence() -> None:
    suite = build_m29_mutation_suite()
    for case in suite.cases:
        assert case.prompt.endswith("\n\n" + M29_MUTATION_PROMPT_TRAILER)


def test_suite_oracle_hash_consistency() -> None:
    suite = build_m29_mutation_suite()
    for case in suite.cases:
        expected_hash = hashlib.sha256(case.acceptance_oracle.encode("utf-8")).hexdigest()
        assert case.acceptance_oracle_sha256 == expected_hash


def test_build_m29_mutation_suite_custom_arguments() -> None:
    custom_rev = "a" * 40
    custom_evaluator = NeutralEvaluatorManifest(
        provider="custom-neutral",
        model="custom-oracle-model",
        system_prompt_sha256="b" * 64,
        tools=("read_file",),
        runtime="read_only",
        fallback_policy="none",
        budget=EvaluatorBudget(timeout_seconds=450, max_tokens=4096),
    )
    custom_budget = MutationTaskBudget(timeout_seconds=900, max_cost_usd=5.0)

    suite = build_m29_mutation_suite(
        target_repository_revision=custom_rev,
        neutral_evaluator=custom_evaluator,
        task_budget=custom_budget,
    )
    assert suite.target_repository_revision == custom_rev
    assert suite.neutral_evaluator == custom_evaluator
    for case in suite.cases:
        assert case.target_repository_revision == custom_rev
        assert case.neutral_evaluator == custom_evaluator
        assert case.task_budget == custom_budget


def _mutate_suite(mutation: Any) -> None:
    suite = build_m29_mutation_suite()
    data = suite.model_dump(mode="json")
    mutation(data)
    M29MutationSuite.model_validate(data)


def test_rejection_wrong_case_count() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"].pop()

    with pytest.raises(ValueError, match="must contain exactly 20 cases"):
        _mutate_suite(mutate)


def test_rejection_duplicate_case_id() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][1]["case_id"] = data["cases"][0]["case_id"]

    with pytest.raises(ValueError, match="duplicate case IDs found"):
        _mutate_suite(mutate)


def test_rejection_candidate_identities_missing_profile() -> None:
    def mutate(data: dict[str, Any]) -> None:
        del data["candidate_identities"]["codex-native-executor"]

    with pytest.raises(ValueError, match="must contain exactly both canonical provider profiles"):
        _mutate_suite(mutate)


def test_rejection_candidate_identity_mismatch() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["candidate_identities"]["codex-native-executor"]["model"] = "gpt-5.6-luna"

    with pytest.raises(ValueError, match="does not match frozen identity"):
        _mutate_suite(mutate)


def test_rejection_evaluator_is_candidate_provider() -> None:
    with pytest.raises(ValueError, match="cannot be a candidate provider"):
        NeutralEvaluatorManifest(
            provider="codex",
            model="neutral-model",
            system_prompt_sha256="c" * 64,
            fallback_policy="none",
        )

    with pytest.raises(ValueError, match="cannot be a candidate provider"):
        NeutralEvaluatorManifest(
            provider="antigravity",
            model="neutral-model",
            system_prompt_sha256="c" * 64,
            fallback_policy="none",
        )


def test_rejection_evaluator_fallback_policy_not_none() -> None:
    with pytest.raises(ValidationError):
        NeutralEvaluatorManifest(
            provider="neutral",
            model="neutral-model",
            system_prompt_sha256="c" * 64,
            fallback_policy="provider_fallback",  # type: ignore[arg-type]
        )


def test_rejection_evaluator_runtime_not_read_only() -> None:
    with pytest.raises(ValidationError):
        NeutralEvaluatorManifest(
            provider="neutral",
            model="neutral-model",
            system_prompt_sha256="c" * 64,
            runtime="mutable",  # type: ignore[arg-type]
        )


def test_rejection_wrong_number_of_pair_groups() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][2]["pair_group"] = data["cases"][0]["pair_group"]
        data["cases"][3]["pair_group"] = data["cases"][0]["pair_group"]

    with pytest.raises(ValueError, match="expected exactly 10 pair groups"):
        _mutate_suite(mutate)


def test_rejection_wrong_cases_per_pair() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["pair_group"] = data["cases"][2]["pair_group"]

    with pytest.raises(ValueError, match="must contain exactly 2 cases"):
        _mutate_suite(mutate)


def test_rejection_wrong_pair_orders() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][1]["pair_order"] = 1

    with pytest.raises(ValueError, match="must use pair_order values 1 and 2"):
        _mutate_suite(mutate)


def test_rejection_pair_missing_provider() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][1]["worker_profile"] = "codex-native-executor"

    with pytest.raises(ValueError, match="must contain one Codex and one Antigravity case"):
        _mutate_suite(mutate)


def test_validate_evaluator_manifest_edge_cases() -> None:
    from evaluation.m29_mutation_suite import _validate_evaluator_manifest

    eval_codex = NeutralEvaluatorManifest.model_construct(
        provider="codex",
        model="m",
        system_prompt_sha256="a" * 64,
        tools=("read_file",),
        runtime="read_only",
        fallback_policy="none",
        budget=EvaluatorBudget(),
    )
    with pytest.raises(ValueError, match="cannot be a candidate provider"):
        _validate_evaluator_manifest(eval_codex)

    eval_fallback = NeutralEvaluatorManifest.model_construct(
        provider="neutral",
        model="m",
        system_prompt_sha256="a" * 64,
        tools=("read_file",),
        runtime="read_only",
        fallback_policy="fallback",
        budget=EvaluatorBudget(),
    )
    with pytest.raises(ValueError, match="neutral evaluator fallback policy must be 'none'"):
        _validate_evaluator_manifest(eval_fallback)

    eval_runtime = NeutralEvaluatorManifest.model_construct(
        provider="neutral",
        model="m",
        system_prompt_sha256="a" * 64,
        tools=("read_file",),
        runtime="mutable",
        fallback_policy="none",
        budget=EvaluatorBudget(),
    )
    with pytest.raises(ValueError, match="neutral evaluator runtime must be 'read_only'"):
        _validate_evaluator_manifest(eval_runtime)


def test_rejection_broken_counterbalancing() -> None:
    def mutate(data: dict[str, Any]) -> None:
        # Swap pair 2 orders so both pair 1 and pair 2 have codex first
        data["cases"][2]["worker_profile"] = "codex-native-executor"
        data["cases"][3]["worker_profile"] = "antigravity-native-executor"

    with pytest.raises(ValueError, match="does not alternate provider order"):
        _mutate_suite(mutate)


def test_rejection_mismatched_prompts_in_pair() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["prompt"] = "Different prompt text\n\n" + M29_MUTATION_PROMPT_TRAILER

    with pytest.raises(ValueError, match="has mismatched prompts"):
        _mutate_suite(mutate)


def test_rejection_missing_prompt_trailer() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["prompt"] = "Prompt without trailer."
        data["cases"][1]["prompt"] = "Prompt without trailer."

    with pytest.raises(ValueError, match="prompt missing required M29 prompt trailer"):
        _mutate_suite(mutate)


def test_rejection_mismatched_oracles_in_pair() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["acceptance_oracle"] = "Different oracle 1"
        data["cases"][1]["acceptance_oracle"] = "Different oracle 2"
        data["cases"][0]["acceptance_oracle_sha256"] = hashlib.sha256(
            b"Different oracle 1"
        ).hexdigest()
        data["cases"][1]["acceptance_oracle_sha256"] = hashlib.sha256(
            b"Different oracle 2"
        ).hexdigest()

    with pytest.raises(ValueError, match="has mismatched acceptance oracles"):
        _mutate_suite(mutate)


def test_rejection_mismatched_oracle_hashes_in_pair() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["acceptance_oracle_sha256"] = "d" * 64

    with pytest.raises(ValueError, match="has mismatched acceptance oracle hashes"):
        _mutate_suite(mutate)


def test_rejection_corrupted_oracle_hash() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["acceptance_oracle_sha256"] = "e" * 64
        data["cases"][1]["acceptance_oracle_sha256"] = "e" * 64

    with pytest.raises(ValueError, match="acceptance oracle sha256 does not match oracle text"):
        _mutate_suite(mutate)


def test_rejection_mismatched_allowed_source_path() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["allowed_source_path"] = "taskboard/other.py"

    with pytest.raises(ValueError, match="has mismatched allowed source paths"):
        _mutate_suite(mutate)


def test_rejection_mismatched_entrypoint() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["entrypoint"] = "other_entrypoint()"

    with pytest.raises(ValueError, match="has mismatched entrypoints"):
        _mutate_suite(mutate)


def test_rejection_mismatched_topic() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["topic"] = "different_topic"

    with pytest.raises(ValueError, match="has mismatched topics"):
        _mutate_suite(mutate)


def test_rejection_mismatched_target_revision() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["target_repository_revision"] = "f" * 40

    with pytest.raises(ValueError, match="has mismatched target repository revisions"):
        _mutate_suite(mutate)


def test_rejection_wrong_candidate_timeout() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["candidate_timeout_seconds"] = 600
        data["cases"][1]["candidate_timeout_seconds"] = 600

    with pytest.raises(ValueError, match="timeout must be 900s"):
        _mutate_suite(mutate)


def test_rejection_mismatched_candidate_timeout() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][1]["candidate_timeout_seconds"] = 901

    with pytest.raises(ValueError, match="timeout must be 900s|mismatched candidate timeouts"):
        _mutate_suite(mutate)


def test_rejection_mismatched_task_budget() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["task_budget"] = {"timeout_seconds": 900, "max_cost_usd": 10.0}

    with pytest.raises(ValueError, match="has mismatched task budgets"):
        _mutate_suite(mutate)


def test_rejection_mismatched_neutral_evaluator() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["neutral_evaluator"]["model"] = "other-eval-model"

    with pytest.raises(ValueError, match="has mismatched neutral evaluator manifests"):
        _mutate_suite(mutate)


def test_rejection_delivery_mode_not_workspace() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["delivery_mode"] = "branch"

    with pytest.raises(ValueError, match="delivery_mode must be 'workspace'"):
        _mutate_suite(mutate)


def test_rejection_missing_modify_workspace_files_action() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["allowed_actions"] = ["read_only_action"]
        data["cases"][1]["allowed_actions"] = ["read_only_action"]

    with pytest.raises(ValueError, match="must allow 'modify_workspace_files'"):
        _mutate_suite(mutate)


def test_rejection_forbidden_delivery_action() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["allowed_actions"] = ["modify_workspace_files", "push_branch"]
        data["cases"][1]["allowed_actions"] = ["modify_workspace_files", "push_branch"]

    with pytest.raises(ValueError, match="contains forbidden delivery action"):
        _mutate_suite(mutate)


def test_rejection_mismatched_allowed_actions() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["allowed_actions"] = ["modify_workspace_files", "extra_action"]

    with pytest.raises(ValueError, match="has mismatched allowed actions"):
        _mutate_suite(mutate)


def test_rejection_non_empty_verification_commands() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["verification_commands"] = ["pytest"]

    with pytest.raises(ValueError, match="must have empty workflow verification commands"):
        _mutate_suite(mutate)


def test_rejection_requires_clarification() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["requires_clarification"] = True

    with pytest.raises(ValueError, match="cannot require clarification"):
        _mutate_suite(mutate)


def test_rejection_requires_permission() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["cases"][0]["requires_permission"] = True

    with pytest.raises(ValueError, match="cannot require permission"):
        _mutate_suite(mutate)


def test_rejection_invalid_target_repository_revision_format() -> None:
    with pytest.raises(ValidationError):
        build_m29_mutation_suite(target_repository_revision="invalid-sha")


def test_rejection_invalid_allowed_source_path() -> None:
    with pytest.raises(ValidationError):
        M29MutationCase(
            case_id="m29-mut-invalid-path",
            worker_profile="codex-native-executor",
            topic="test",
            pair_group="test-group",
            pair_order=1,
            prompt="Prompt\n\n" + M29_MUTATION_PROMPT_TRAILER,
            allowed_source_path="invalid/path.py",
            entrypoint="test()",
            acceptance_oracle="oracle",
            acceptance_oracle_sha256=hashlib.sha256(b"oracle").hexdigest(),
            target_repository_revision="0" * 40,
            neutral_evaluator=DEFAULT_M29_NEUTRAL_EVALUATOR,
        )


def test_raw_pair_definitions_contain_ten_unique_pairs() -> None:
    assert len(RAW_PAIR_DEFINITIONS) == 10
    topics = [raw[0] for raw in RAW_PAIR_DEFINITIONS]
    assert len(set(topics)) == 10
    slugs = [raw[5] for raw in RAW_PAIR_DEFINITIONS]
    assert len(set(slugs)) == 10
