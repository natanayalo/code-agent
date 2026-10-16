"""Frozen M29 feature/mutation suite and neutral evaluator manifest contract."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evaluation.m29_mutation_cohort import M29_MUTATION_PROFILES
from evaluation.provider_reliability_models import ExecutionIdentity

M29_MUTATION_SUITE_NAME = "m29-live-provider-evidence-mutation"
CANDIDATE_TIMEOUT_SECONDS = 900
DEFAULT_FIXTURE_REVISION = "0000000000000000000000000000000000000000"

M29_MUTATION_PROMPT_TRAILER = (
    "Make the smallest implementation for this request in the task workspace. "
    "Do not change unrelated files or add dependencies. "
    "Report the files changed and any blockers. "
    "Do not modify or invoke runner-owned evaluation checks."
)

NEUTRAL_EVALUATOR_SYSTEM_PROMPT = (
    "You are an independent, read-only code quality evaluator. "
    "Evaluate task implementation against the acceptance criteria without modifying files."
)

M29_FROZEN_MUTATION_CANDIDATE_IDENTITIES: dict[str, ExecutionIdentity] = {
    "codex-native-executor": ExecutionIdentity(
        provider="codex", model="gpt-6-luna", reasoning_effort="high"
    ),
    "antigravity-native-executor": ExecutionIdentity(
        provider="antigravity", model="gemini-3.8-flash", reasoning_effort="medium"
    ),
}

M29_MUTATION_PROVIDERS = ("codex", "antigravity")
M29MutationProfile = Literal["codex-native-executor", "antigravity-native-executor"]


class StrictModel(BaseModel):
    """Reject undeclared or private fields in evaluation models."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvaluatorBudget(StrictModel):
    """Equal per-case limits for neutral third-party model evaluation."""

    timeout_seconds: int = Field(default=300, ge=1)
    max_tokens: int = Field(default=8192, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0.0)


class NeutralEvaluatorManifest(StrictModel):
    """Neutral third-party evaluator manifest pinned across all 20 cases."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str | None = None
    system_prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tools: tuple[str, ...] = ("read_file", "list_dir")
    runtime: Literal["read_only"] = "read_only"
    fallback_policy: Literal["none"] = "none"
    budget: EvaluatorBudget = Field(default_factory=EvaluatorBudget)

    @model_validator(mode="after")
    def validate_evaluator(self) -> NeutralEvaluatorManifest:
        if self.provider in M29_MUTATION_PROVIDERS:
            raise ValueError(
                f"neutral evaluator provider '{self.provider}' cannot be a candidate provider"
            )
        return self


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


DEFAULT_M29_NEUTRAL_EVALUATOR = NeutralEvaluatorManifest(
    provider="anthropic",
    model="claude-3-7-sonnet",
    reasoning_effort=None,
    system_prompt_sha256=_hash_text(NEUTRAL_EVALUATOR_SYSTEM_PROMPT),
    tools=("read_file", "list_dir"),
    runtime="read_only",
    fallback_policy="none",
    budget=EvaluatorBudget(timeout_seconds=300, max_tokens=8192),
)


class MutationTaskBudget(StrictModel):
    """Execution budgets for candidate worker execution."""

    timeout_seconds: int = Field(default=CANDIDATE_TIMEOUT_SECONDS, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0.0)
    max_tokens: int | None = Field(default=None, ge=1)


class M29MutationCase(StrictModel):
    """Single frozen feature/mutation case specification."""

    case_id: str = Field(pattern=r"^[a-z0-9-]+$")
    task_class: Literal["feature"] = "feature"
    mutation_mode: Literal["mutation"] = "mutation"
    worker_profile: M29MutationProfile
    topic: str = Field(pattern=r"^[a-z0-9_]+$")
    pair_group: str = Field(pattern=r"^[a-z0-9-]+$")
    pair_order: int = Field(ge=1, le=2)
    prompt: str = Field(min_length=10)
    allowed_source_path: str = Field(pattern=r"^taskboard/[a-z_]+\.py$")
    entrypoint: str = Field(min_length=3)
    acceptance_oracle: str = Field(min_length=10)
    acceptance_oracle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_repository_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_timeout_seconds: int = Field(default=CANDIDATE_TIMEOUT_SECONDS, ge=1)
    task_budget: MutationTaskBudget = Field(default_factory=MutationTaskBudget)
    neutral_evaluator: NeutralEvaluatorManifest
    delivery_mode: str = "workspace"
    allowed_actions: tuple[str, ...] = ("modify_workspace_files",)
    verification_commands: tuple[str, ...] = ()
    requires_clarification: bool = False
    clarification_questions: tuple[str, ...] = ()
    requires_permission: bool = False


@dataclass(frozen=True)
class M29MutationPair:
    """Convenience representation of a matched pair in the mutation suite."""

    pair_index: int
    pair_group: str
    topic: str
    allowed_source_path: str
    entrypoint: str
    prompt: str
    acceptance_oracle: str
    acceptance_oracle_sha256: str
    first_profile: str
    second_profile: str
    cases: tuple[M29MutationCase, M29MutationCase]


def _validate_suite_counts_and_ids(suite: M29MutationSuite) -> None:
    if len(suite.cases) != 20:
        raise ValueError(
            f"M29 mutation suite must contain exactly 20 cases, got {len(suite.cases)}"
        )
    case_ids = [c.case_id for c in suite.cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("duplicate case IDs found in M29 mutation suite")
    if set(suite.candidate_identities) != set(M29_MUTATION_PROFILES):
        raise ValueError(
            "candidate identities must contain exactly both canonical provider profiles"
        )
    for profile in M29_MUTATION_PROFILES:
        expected = M29_FROZEN_MUTATION_CANDIDATE_IDENTITIES[profile]
        actual = suite.candidate_identities[profile]
        if actual != expected:
            raise ValueError(
                f"candidate identity for {profile} does not match frozen identity: "
                f"expected={expected} actual={actual}"
            )


def _validate_evaluator_manifest(evaluator: NeutralEvaluatorManifest) -> None:
    if evaluator.provider in M29_MUTATION_PROVIDERS:
        raise ValueError(
            f"neutral evaluator provider '{evaluator.provider}' cannot be a candidate provider"
        )
    if evaluator.fallback_policy != "none":
        raise ValueError(
            f"neutral evaluator fallback policy must be 'none', got '{evaluator.fallback_policy}'"
        )
    if evaluator.runtime != "read_only":
        raise ValueError("neutral evaluator runtime must be 'read_only'")


def _validate_case_suite_bindings(suite: M29MutationSuite) -> None:
    expected_budget = suite.cases[0].task_budget if suite.cases else None
    for c in suite.cases:
        if c.target_repository_revision != suite.target_repository_revision:
            raise ValueError(
                f"case '{c.case_id}' target_repository_revision does not match suite revision: "
                f"{c.target_repository_revision} != {suite.target_repository_revision}"
            )
        if c.neutral_evaluator != suite.neutral_evaluator:
            raise ValueError(
                f"case '{c.case_id}' neutral_evaluator does not match suite neutral_evaluator"
            )
        if c.task_budget != expected_budget:
            raise ValueError(f"case '{c.case_id}' task_budget does not match suite task_budget")
        if c.task_budget.timeout_seconds != CANDIDATE_TIMEOUT_SECONDS:
            raise ValueError(
                f"case '{c.case_id}' task_budget timeout must be {CANDIDATE_TIMEOUT_SECONDS}s, "
                f"got {c.task_budget.timeout_seconds}s"
            )
        if c.candidate_timeout_seconds != CANDIDATE_TIMEOUT_SECONDS:
            raise ValueError(
                f"case '{c.case_id}' timeout must be {CANDIDATE_TIMEOUT_SECONDS}s, "
                f"got {c.candidate_timeout_seconds}s"
            )
        if c.clarification_questions != ():
            raise ValueError(f"case '{c.case_id}' cannot have clarification questions")


def _validate_pair_grouping_and_counterbalancing(
    cases: Sequence[M29MutationCase],
) -> dict[str, tuple[M29MutationCase, M29MutationCase]]:
    if len(cases) != 20:
        raise ValueError(f"expected exactly 20 cases, got {len(cases)}")

    pairs: dict[str, tuple[M29MutationCase, M29MutationCase]] = {}
    for i in range(10):
        c1, c2 = cases[2 * i], cases[2 * i + 1]
        if c1.pair_group != c2.pair_group:
            raise ValueError(
                f"consecutive cases at indices {2 * i} and {2 * i + 1} "
                "must share the same pair group"
            )
        if c1.pair_group in pairs:
            raise ValueError(f"duplicate pair group '{c1.pair_group}' in suite")
        if (c1.pair_order, c2.pair_order) != (1, 2):
            raise ValueError(
                f"pair group '{c1.pair_group}' cases must be ordered with pair_order (1, 2)"
            )
        if {c1.worker_profile, c2.worker_profile} != set(M29_MUTATION_PROFILES):
            raise ValueError(
                f"pair group '{c1.pair_group}' must contain one Codex and one Antigravity case"
            )
        if i == 0 and c1.worker_profile != "codex-native-executor":
            raise ValueError(
                f"Pair 01 '{c1.pair_group}' must start with codex-native-executor, "
                f"got {c1.worker_profile}"
            )
        if i > 0 and c1.worker_profile == cases[2 * (i - 1)].worker_profile:
            raise ValueError(
                f"pair group '{c1.pair_group}' does not alternate provider order "
                "to counterbalance ordering bias"
            )
        pairs[c1.pair_group] = (c1, c2)

    return pairs


def _validate_pair_semantic_fields(
    pair_group: str,
    pair_cases: tuple[M29MutationCase, M29MutationCase],
) -> None:
    c1, c2 = pair_cases[0], pair_cases[1]

    if c1.prompt != c2.prompt:
        raise ValueError(f"pair group '{pair_group}' has mismatched prompts")
    if not c1.prompt.endswith("\n\n" + M29_MUTATION_PROMPT_TRAILER):
        raise ValueError(f"case '{c1.case_id}' prompt missing required M29 prompt trailer")
    if c1.acceptance_oracle != c2.acceptance_oracle:
        raise ValueError(f"pair group '{pair_group}' has mismatched acceptance oracles")
    if c1.acceptance_oracle_sha256 != c2.acceptance_oracle_sha256:
        raise ValueError(f"pair group '{pair_group}' has mismatched acceptance oracle hashes")
    if c1.acceptance_oracle_sha256 != _hash_text(c1.acceptance_oracle):
        raise ValueError(f"case '{c1.case_id}' acceptance oracle sha256 does not match oracle text")
    if c1.allowed_source_path != c2.allowed_source_path:
        raise ValueError(f"pair group '{pair_group}' has mismatched allowed source paths")
    if c1.entrypoint != c2.entrypoint:
        raise ValueError(f"pair group '{pair_group}' has mismatched entrypoints")
    if c1.topic != c2.topic:
        raise ValueError(f"pair group '{pair_group}' has mismatched topics")
    if c1.delivery_mode != "workspace" or c2.delivery_mode != "workspace":
        raise ValueError(f"pair group '{pair_group}' delivery_mode must be 'workspace'")
    if "modify_workspace_files" not in c1.allowed_actions:
        raise ValueError(f"case '{c1.case_id}' must allow 'modify_workspace_files'")
    if any("deliver" in act or "branch" in act or "pr" in act for act in c1.allowed_actions):
        raise ValueError(f"case '{c1.case_id}' contains forbidden delivery action")
    if c1.allowed_actions != c2.allowed_actions:
        raise ValueError(f"pair group '{pair_group}' has mismatched allowed actions")
    if c1.verification_commands != () or c2.verification_commands != ():
        raise ValueError(
            f"pair group '{pair_group}' must have empty workflow verification commands"
        )
    if c1.requires_clarification or c2.requires_clarification:
        raise ValueError(f"pair group '{pair_group}' cannot require clarification")
    if c1.requires_permission or c2.requires_permission:
        raise ValueError(f"pair group '{pair_group}' cannot require permission")


def validate_m29_mutation_suite(suite: M29MutationSuite) -> None:
    """Validate all frozen contract rules for the M29 feature/mutation suite."""
    _validate_suite_counts_and_ids(suite)
    _validate_evaluator_manifest(suite.neutral_evaluator)
    _validate_case_suite_bindings(suite)
    pairs = _validate_pair_grouping_and_counterbalancing(suite.cases)
    for pair_group, pair_cases in pairs.items():
        _validate_pair_semantic_fields(pair_group, pair_cases)


class M29MutationSuite(StrictModel):
    """Frozen M29 feature/mutation evaluation suite contract."""

    suite_name: Literal[M29_MUTATION_SUITE_NAME] = M29_MUTATION_SUITE_NAME
    schema_version: Literal[1] = 1
    target_repository_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_identities: dict[str, ExecutionIdentity] = Field(
        default_factory=lambda: dict(M29_FROZEN_MUTATION_CANDIDATE_IDENTITIES)
    )
    neutral_evaluator: NeutralEvaluatorManifest
    cases: list[M29MutationCase]

    @model_validator(mode="after")
    def validate_contract(self) -> M29MutationSuite:
        validate_m29_mutation_suite(self)
        return self

    def compute_digest(self) -> str:
        """Return deterministic SHA-256 of the suite's canonical JSON representation."""
        canonical_json = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def case_ids_by_profile(self) -> dict[str, list[str]]:
        """Return scheduled case IDs mapped by candidate profile."""
        return {
            p: [c.case_id for c in self.cases if c.worker_profile == p]
            for p in M29_MUTATION_PROFILES
        }

    def cases_by_profile(self) -> dict[str, list[M29MutationCase]]:
        """Return cases mapped by candidate profile."""
        return {p: [c for c in self.cases if c.worker_profile == p] for p in M29_MUTATION_PROFILES}

    def pair_cases(self) -> list[tuple[M29MutationCase, M29MutationCase]]:
        """Return sorted pairs of cases in order of scheduled execution."""
        return [(self.cases[2 * i], self.cases[2 * i + 1]) for i in range(10)]


RAW_PAIR_DEFINITIONS: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "label_normalization",
        "taskboard/labels.py",
        "normalize_label(value: str) -> str",
        (
            "Implement normalize_label so it applies Unicode NFKC normalization and case folding, "
            "replaces each run of non-alphanumeric characters with one hyphen, "
            "and removes leading and trailing hyphens."
        ),
        (
            '"  Quick   Fix! " becomes "quick-fix"; "café & TEA" becomes "café-tea"; '
            'full-width "ＦＯＯ" becomes "foo"; punctuation-only input becomes the empty string. '
            "The input is not mutated."
        ),
        "label-norm",
    ),
    (
        "task_ordering",
        "taskboard/ordering.py",
        "sort_tasks(tasks: list[Task]) -> list[Task]",
        (
            "Implement sort_tasks to order tasks by descending priority, "
            "then ascending created_at, then ascending task id for ties. "
            "Return a new list and leave the input order unchanged."
        ),
        (
            'For input IDs ["b", "a", "c", "d"], where b and a have priority 2 and the same '
            "timestamp, c has priority 3 and a later timestamp, and d has priority 2 and an "
            'earlier timestamp, return IDs ["c", "d", "a", "b"]. Verify the input remains '
            '["b", "a", "c", "d"].'
        ),
        "task-order",
    ),
    (
        "task_filters",
        "taskboard/filters.py",
        "filter_tasks(tasks, *, status=None, owner=None, tag=None) -> list[Task]",
        (
            "Implement optional task filters for exact status, case-insensitive owner, "
            "and exact tag. When more than one filter is supplied, require all of them to match. "
            "Preserve source order and leave the input unchanged."
        ),
        (
            "Use tasks a=(queued, Sam, [red]), b=(done, sam, [red]), "
            "c=(queued, Lee, [red, blue]), and d=(in_progress, SAM, [blue]), in that order. "
            "Status queued returns [a, c]; owner SAM returns [a, b, d]; tag red returns [a, b, c]; "
            "all three filters set to queued/sAm/red return [a]; done/Lee returns []; "
            "no filters returns all four in their original order."
        ),
        "task-filters",
    ),
    (
        "cursor_pagination",
        "taskboard/pagination.py",
        "paginate_tasks(tasks, *, limit: int, after_id: str | None = None) -> Page",
        (
            "Implement exclusive cursor pagination over the supplied task order. "
            "The cursor is the ID of the last item on the previous page. Return a Page with the "
            "page items and a next_cursor set to the last returned ID only when more items remain."
        ),
        (
            "Cover first, middle, final, empty, and single-page requests over IDs [a, b, c, d]: "
            "limit 2 returns [a, b] with cursor b, then after b returns [c, d] with no cursor; "
            "limit 4 returns all items with no cursor; after d returns an empty page. "
            "A non-positive limit or unknown cursor raises ValueError; "
            "the cursor item is not repeated."
        ),
        "cursor-page",
    ),
    (
        "csv_export",
        "taskboard/export.py",
        "export_tasks_csv(tasks: list[Task]) -> str",
        (
            "Implement an in-memory CSV export with the fixed header "
            "id,title,status,owner,priority, preserving task order and emitting an empty field "
            "for a missing owner. Use standard CSV escaping and LF line endings."
        ),
        (
            "Verify the exact header and row order. A title containing a comma round-trips "
            "unchanged; a title containing a quote and newline round-trips unchanged; "
            "a missing owner parses as an empty field. Assert all record separators are LF and "
            "the function performs no file I/O."
        ),
        "csv-export",
    ),
    (
        "status_transitions",
        "taskboard/status.py",
        "transition_status(current: str, requested: str) -> str",
        (
            "Implement the task status transition rules: queued may move to in_progress "
            "or cancelled; in_progress may move to done or cancelled; done and cancelled "
            "are terminal. A request for the current known status is an idempotent no-op."
        ),
        (
            "Verify every allowed edge, same-state no-op for each known status, "
            "and rejection of every other or unknown state with ValueError."
        ),
        "status-trans",
    ),
    (
        "capped_retry_delay",
        "taskboard/retry.py",
        "retry_delay_seconds(attempt: int, *, base_seconds: int = 1, max_seconds: int = 30) -> int",
        (
            "Implement deterministic exponential retry delay as "
            "min(max_seconds, base_seconds * 2 ** (attempt - 1)). Attempts start at one; "
            "base and maximum delays may be zero but cannot be negative. Return the maximum "
            "delay without constructing an unbounded integer for a very large attempt."
        ),
        (
            "With defaults, attempts 1, 2, 3, and 6 return 1, 2, 4, and 30. "
            "Verify a custom base and cap, zero delay, a very large attempt, "
            "and ValueError for an attempt below one or a negative delay."
        ),
        "retry-delay",
    ),
    (
        "event_deduplication",
        "taskboard/events.py",
        "deduplicate_events(events: list[Event]) -> list[Event]",
        (
            "Implement event deduplication by event ID. Keep the first occurrence of each ID, "
            "preserve the order of those first occurrences, and return a new list "
            "without modifying the input."
        ),
        (
            "Verify repeated IDs with different payloads keep the first event, unique events "
            "keep their order, empty input returns empty, and the input list is unchanged."
        ),
        "event-dedup",
    ),
    (
        "event_counts",
        "taskboard/events.py",
        "count_events_by_type(events: list[Event]) -> dict[str, int]",
        (
            "Implement event counts grouped by the exact event_type string. "
            "Return an empty mapping for no events and a deterministic mapping for non-empty input."
        ),
        (
            'For types ["create", "update", "create", "Create"], '
            'return {"Create": 1, "create": 2, "update": 1} with keys in lexical order. '
            "Empty input returns {} and the event list is unchanged."
        ),
        "event-counts",
    ),
    (
        "task_status_summary",
        "taskboard/summary.py",
        "summarize_tasks(tasks: list[Task]) -> dict[str, int]",
        (
            "Implement summarize_tasks to return counts for total, queued, in_progress, done, "
            "cancelled, and other. Count every task in total, count known statuses in their own "
            "field, and count unrecognized statuses in other. Return all six keys even when "
            "their counts are zero."
        ),
        (
            'For statuses ["queued", "done", "done", "cancelled", "archived"], return '
            '{"total": 5, "queued": 1, "in_progress": 0, "done": 2, "cancelled": 1, "other": 1}. '
            "Empty input returns all six keys with zero values; the task list is unchanged."
        ),
        "task-summary",
    ),
)


def _build_cases_for_pair(
    pair_index: int,
    raw_def: tuple[str, str, str, str, str, str],
    target_repository_revision: str,
    neutral_evaluator: NeutralEvaluatorManifest,
    task_budget: MutationTaskBudget,
) -> tuple[M29MutationCase, M29MutationCase]:
    topic, path, entrypoint, prompt_base, oracle, slug = raw_def
    prompt = f"{prompt_base}\n\n{M29_MUTATION_PROMPT_TRAILER}"
    oracle_hash = _hash_text(oracle)
    pair_group = f"m29-mut-pair-{pair_index:02d}-{slug}"

    profiles: tuple[tuple[M29MutationProfile, str], ...]
    if pair_index % 2 == 1:
        profiles = (
            ("codex-native-executor", "codex"),
            ("antigravity-native-executor", "antigravity"),
        )
    else:
        profiles = (
            ("antigravity-native-executor", "antigravity"),
            ("codex-native-executor", "codex"),
        )

    cases = [
        M29MutationCase(
            case_id=f"m29-mut-{pair_index:02d}-{slug}-{p_slug}",
            worker_profile=profile,
            topic=topic,
            pair_group=pair_group,
            pair_order=order,
            prompt=prompt,
            allowed_source_path=path,
            entrypoint=entrypoint,
            acceptance_oracle=oracle,
            acceptance_oracle_sha256=oracle_hash,
            target_repository_revision=target_repository_revision,
            candidate_timeout_seconds=CANDIDATE_TIMEOUT_SECONDS,
            task_budget=task_budget,
            neutral_evaluator=neutral_evaluator,
        )
        for order, (profile, p_slug) in enumerate(profiles, start=1)
    ]
    return cases[0], cases[1]


def build_m29_mutation_suite(
    target_repository_revision: str = DEFAULT_FIXTURE_REVISION,
    neutral_evaluator: NeutralEvaluatorManifest | None = None,
    task_budget: MutationTaskBudget | None = None,
) -> M29MutationSuite:
    """Build the canonical frozen 20-case, 10-pair M29 feature/mutation suite."""
    evaluator = neutral_evaluator or DEFAULT_M29_NEUTRAL_EVALUATOR
    budget = task_budget or MutationTaskBudget()

    cases: list[M29MutationCase] = []
    for idx, raw_def in enumerate(RAW_PAIR_DEFINITIONS, start=1):
        c1, c2 = _build_cases_for_pair(idx, raw_def, target_repository_revision, evaluator, budget)
        cases.extend([c1, c2])

    return M29MutationSuite(
        target_repository_revision=target_repository_revision,
        neutral_evaluator=evaluator,
        cases=cases,
    )


FROZEN_SUITE_PATH = Path(__file__).resolve().parent / "m29_live_provider_suite_mutation.json"


def load_frozen_mutation_suite(path: Path | None = None) -> M29MutationSuite:
    """Load and validate the frozen mutation suite from disk."""
    suite_path = path or FROZEN_SUITE_PATH
    raw_data = json.loads(suite_path.read_text(encoding="utf-8"))
    return M29MutationSuite.model_validate(raw_data)
