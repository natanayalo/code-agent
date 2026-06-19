"""Environment-driven task-service bootstrap for the API app."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from apps.api.progress import (
    CompositeProgressNotifier,
    OutboundHttpClients,
    TelegramProgressNotifier,
    WebhookCallbackProgressNotifier,
)
from apps.runtime import coerce_positive_int_env as _coerce_positive_int
from db.enums import WorkerRuntimeMode
from orchestrator.brain import RuleBasedOrchestratorBrain
from orchestrator.execution import ProgressNotifier, TaskExecutionService
from repositories import create_engine_from_url, create_session_factory
from sandbox import DockerSandboxContainerManager
from sandbox.workspace import default_workspace_root
from workers import (
    AntigravityCliRuntimeAdapter,
    CodexCliWorker,
    CodexExecCliRuntimeAdapter,
    GeminiCliRuntimeAdapter,
    GeminiCliWorker,
    OpenRouterCliRuntimeAdapter,
    OpenRouterCliWorker,
    ShellWorker,
    Worker,
    WorkerProfile,
    WorkerType,
)
from workers.antigravity_cli_adapter import (
    ANTIGRAVITY_ARTIFACT_REVIEW_POLICY_ENV_VAR,
    ANTIGRAVITY_AUTH_DIR_ENV_VAR,
    ANTIGRAVITY_EXECUTABLE_ENV_VAR,
    ANTIGRAVITY_MODEL_ENV_VAR,
    ANTIGRAVITY_NATIVE_SANDBOX_ENABLED_ENV_VAR,
    ANTIGRAVITY_TIMEOUT_ENV_VAR,
    ANTIGRAVITY_TOOL_PERMISSION_ENV_VAR,
)
from workers.codex_exec_adapter import CODEX_SANDBOX_ENV_VAR
from workers.gemini_cli_adapter import (
    GEMINI_EXECUTABLE_ENV_VAR,
    GEMINI_MODEL_ENV_VAR,
    GEMINI_TIMEOUT_ENV_VAR,
)
from workers.openrouter_adapter import OPENROUTER_API_KEY_ENV_VAR

ENABLE_TASK_SERVICE_ENV_VAR: Final[str] = "CODE_AGENT_ENABLE_TASK_SERVICE"
DEFAULT_TASK_MAX_ATTEMPTS_ENV_VAR: Final[str] = "CODE_AGENT_QUEUE_MAX_ATTEMPTS"
DATABASE_URL_ENV_VAR: Final[str] = "DATABASE_URL"
DATABASE_DRIVER_ENV_VAR: Final[str] = "DATABASE_DRIVER"
DATABASE_HOST_ENV_VAR: Final[str] = "DATABASE_HOST"
DATABASE_PORT_ENV_VAR: Final[str] = "DATABASE_PORT"
DATABASE_NAME_ENV_VAR: Final[str] = "POSTGRES_DB"
DATABASE_USER_ENV_VAR: Final[str] = "POSTGRES_USER"
DATABASE_PASSWORD_ENV_VAR: Final[str] = "POSTGRES_PASSWORD"
DEFAULT_DATABASE_DRIVER: Final[str] = "postgresql+psycopg"
TELEGRAM_BOT_TOKEN_ENV_VAR: Final[str] = "CODE_AGENT_TELEGRAM_BOT_TOKEN"
TELEGRAM_API_BASE_URL_ENV_VAR: Final[str] = "CODE_AGENT_TELEGRAM_API_BASE_URL"
CHECKPOINT_DB_PATH_ENV_VAR: Final[str] = "CODE_AGENT_CHECKPOINT_DB_PATH"
WORKSPACE_ROOT_ENV_VAR: Final[str] = "CODE_AGENT_WORKSPACE_ROOT"
SANDBOX_IMAGE_ENV_VAR: Final[str] = "CODE_AGENT_SANDBOX_IMAGE"
WORKER_PROFILES_ENABLED_ENV_VAR: Final[str] = "CODE_AGENT_WORKER_PROFILES_ENABLED"
CODEX_RUNTIME_MODE_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_RUNTIME_MODE"
GEMINI_RUNTIME_MODE_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_RUNTIME_MODE"
OPENROUTER_ENABLED_ENV_VAR: Final[str] = "CODE_AGENT_OPENROUTER_ENABLED"
NATIVE_AGENT_EVENT_CAPTURE_ENABLED_ENV_VAR: Final[str] = (
    "CODE_AGENT_NATIVE_AGENT_EVENT_CAPTURE_ENABLED"
)
INDEPENDENT_VERIFIER_ENABLED_ENV_VAR: Final[str] = "CODE_AGENT_INDEPENDENT_VERIFIER_ENABLED"
ORCHESTRATOR_BRAIN_ENABLED_ENV_VAR: Final[str] = "CODE_AGENT_ORCHESTRATOR_BRAIN_ENABLED"
IMPROVEMENT_LLM_SCORING_ENABLED_ENV_VAR: Final[str] = "CODE_AGENT_IMPROVEMENT_LLM_SCORING_ENABLED"
CODEX_TOOL_LOOP_LEGACY_ENABLED_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_TOOL_LOOP_LEGACY_ENABLED"
GEMINI_TOOL_LOOP_LEGACY_ENABLED_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_TOOL_LOOP_LEGACY_ENABLED"
CODEX_TRUSTED_REPO_PATTERNS_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_TRUSTED_REPO_PATTERNS"
GEMINI_NATIVE_SANDBOX_ENABLED_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_NATIVE_SANDBOX_ENABLED"

ANTIGRAVITY_CONFIG_ENV_VARS: Final[tuple[str, ...]] = (
    ANTIGRAVITY_EXECUTABLE_ENV_VAR,
    ANTIGRAVITY_MODEL_ENV_VAR,
    ANTIGRAVITY_TIMEOUT_ENV_VAR,
    ANTIGRAVITY_NATIVE_SANDBOX_ENABLED_ENV_VAR,
    ANTIGRAVITY_TOOL_PERMISSION_ENV_VAR,
    ANTIGRAVITY_ARTIFACT_REVIEW_POLICY_ENV_VAR,
)
GEMINI_CONFIG_ENV_VARS: Final[tuple[str, ...]] = (
    GEMINI_EXECUTABLE_ENV_VAR,
    GEMINI_MODEL_ENV_VAR,
    GEMINI_TIMEOUT_ENV_VAR,
)
ANTIGRAVITY_LEGACY_GEMINI_EXECUTABLE_NAMES: Final[frozenset[str]] = frozenset(
    {"agy", "antigravity"}
)

# Default profile names
ANTIGRAVITY_NATIVE_PLANNER_PROFILE: Final[str] = "antigravity-native-planner"
ANTIGRAVITY_NATIVE_REVIEWER_PROFILE: Final[str] = "antigravity-native-reviewer"
ANTIGRAVITY_NATIVE_DISCOVERY_PROFILE: Final[str] = "antigravity-native-discovery"
GEMINI_NATIVE_PLANNER_PROFILE: Final[str] = ANTIGRAVITY_NATIVE_PLANNER_PROFILE
GEMINI_NATIVE_REVIEWER_PROFILE: Final[str] = ANTIGRAVITY_NATIVE_REVIEWER_PROFILE
GEMINI_NATIVE_DISCOVERY_PROFILE: Final[str] = ANTIGRAVITY_NATIVE_DISCOVERY_PROFILE

logger = logging.getLogger(__name__)


def _is_enabled(value: str | None) -> bool:
    """Interpret common truthy environment values."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _coerce_runtime_mode(
    value: str | None,
    *,
    default: WorkerRuntimeMode,
) -> WorkerRuntimeMode:
    """Parse supported runtime mode overrides with a safe default."""
    if value is None:
        return default
    normalized = value.strip().lower()
    try:
        return WorkerRuntimeMode(normalized)
    except ValueError:
        # If the value was explicitly provided but is invalid, we should fail fast
        # to avoid confusing behavior.
        raise ValueError(
            f"Invalid worker runtime mode: '{value}'. "
            f"Expected one of: {', '.join([m.value for m in WorkerRuntimeMode])}"
        )


def _coerce_execution_runtime_mode(
    value: str | None,
    *,
    default: WorkerRuntimeMode,
    worker_name: str,
) -> WorkerRuntimeMode:
    """Parse runtime modes and restrict worker execution modes to native/tool-loop."""
    runtime_mode = _coerce_runtime_mode(value, default=default)
    if runtime_mode not in {WorkerRuntimeMode.NATIVE_AGENT, WorkerRuntimeMode.TOOL_LOOP}:
        raise ValueError(
            f"Invalid {worker_name} runtime mode: '{runtime_mode.value}'. "
            "Supported values are: native_agent, tool_loop."
        )
    return runtime_mode


def _resolve_default_runtime_mode(
    value: str | None,
    *,
    default: WorkerRuntimeMode,
    worker_name: str,
    env_var_name: str,
) -> WorkerRuntimeMode:
    """Resolve default worker runtime mode while hard-deprecating tool-loop defaults."""
    runtime_mode = _coerce_execution_runtime_mode(
        value,
        default=default,
        worker_name=worker_name,
    )
    if runtime_mode == WorkerRuntimeMode.TOOL_LOOP:
        logger.warning(
            "Ignoring deprecated %s=%s for %s default runtime mode. "
            "Defaults are pinned to native_agent; enable legacy tool-loop profiles explicitly "
            "and use worker_profile_override for per-task opt-in.",
            env_var_name,
            runtime_mode.value,
            worker_name,
        )
        return default
    return runtime_mode


def _add_worker_profiles(
    profiles: dict[str, WorkerProfile],
    worker_type: WorkerType,
    runtime_mode: WorkerRuntimeMode,
    *,
    legacy_mode: bool = False,
) -> None:
    """Helper to add standard and read-only profiles for a worker."""
    profile_name = (
        f"{worker_type}-native-executor"
        if runtime_mode == WorkerRuntimeMode.NATIVE_AGENT
        else f"{worker_type}-tool-loop-executor"
    )
    metadata = {"legacy_mode": True} if legacy_mode else {}
    # Standard profile
    profiles[profile_name] = WorkerProfile(
        name=profile_name,
        worker_type=worker_type,
        runtime_mode=runtime_mode,
        capability_tags=["execution"],
        supported_delivery_modes=["workspace", "branch", "draft_pr"],
        permission_profile="workspace_write",
        mutation_policy="patch_allowed",
        self_review_policy="on_failure",
        metadata=metadata,
    )
    # Read-only profile
    ro_name = f"{profile_name}-read-only"
    profiles[ro_name] = WorkerProfile(
        name=ro_name,
        worker_type=worker_type,
        runtime_mode=runtime_mode,
        capability_tags=["execution"],
        supported_delivery_modes=["workspace", "branch", "draft_pr"],
        permission_profile="read_only",
        mutation_policy="read_only",
        self_review_policy="on_failure",
        metadata=metadata,
    )


def _build_default_worker_profiles(
    *,
    include_gemini: bool,
    include_openrouter: bool,
    include_codex_legacy_tool_loop: bool,
    include_gemini_legacy_tool_loop: bool,
) -> dict[str, WorkerProfile]:
    """Build the default executable profile map for routing decisions."""
    profiles: dict[str, WorkerProfile] = {}

    _add_worker_profiles(profiles, "codex", WorkerRuntimeMode.NATIVE_AGENT)
    if include_codex_legacy_tool_loop:
        _add_worker_profiles(profiles, "codex", WorkerRuntimeMode.TOOL_LOOP, legacy_mode=True)

    if include_gemini:
        _add_worker_profiles(profiles, "antigravity", WorkerRuntimeMode.NATIVE_AGENT)
        if include_gemini_legacy_tool_loop:
            _add_worker_profiles(
                profiles,
                "antigravity",
                WorkerRuntimeMode.TOOL_LOOP,
                legacy_mode=True,
            )
        profiles[ANTIGRAVITY_NATIVE_PLANNER_PROFILE] = WorkerProfile(
            name=ANTIGRAVITY_NATIVE_PLANNER_PROFILE,
            worker_type="antigravity",
            runtime_mode=WorkerRuntimeMode.PLANNER_ONLY,
            capability_tags=["planning"],
            supported_delivery_modes=["workspace", "branch", "draft_pr"],
            permission_profile="workspace_write",
            mutation_policy="patch_allowed",
            self_review_policy="on_failure",
        )
        profiles[ANTIGRAVITY_NATIVE_DISCOVERY_PROFILE] = WorkerProfile(
            name=ANTIGRAVITY_NATIVE_DISCOVERY_PROFILE,
            worker_type="antigravity",
            runtime_mode=WorkerRuntimeMode.PLANNER_ONLY,
            capability_tags=["planning"],
            supported_delivery_modes=["workspace", "branch", "draft_pr"],
            permission_profile="workspace_write",
            mutation_policy="patch_allowed",
            self_review_policy="on_failure",
        )
        profiles[ANTIGRAVITY_NATIVE_REVIEWER_PROFILE] = WorkerProfile(
            name=ANTIGRAVITY_NATIVE_REVIEWER_PROFILE,
            worker_type="antigravity",
            runtime_mode=WorkerRuntimeMode.REVIEWER_ONLY,
            capability_tags=["review"],
            supported_delivery_modes=["workspace", "branch", "draft_pr"],
            permission_profile="workspace_write",
            mutation_policy="patch_allowed",
            self_review_policy="on_failure",
        )

    if include_openrouter:
        profiles["openrouter-tool-loop-legacy"] = WorkerProfile(
            name="openrouter-tool-loop-legacy",
            worker_type="openrouter",
            runtime_mode=WorkerRuntimeMode.TOOL_LOOP,
            capability_tags=["execution"],
            supported_delivery_modes=["workspace"],
            permission_profile="workspace_write",
            mutation_policy="patch_allowed",
            self_review_policy="on_failure",
        )

    return profiles


def _database_url_from_env(environ: Mapping[str, str]) -> str | None:
    """Resolve a DB URL from either a full URL or the compose-style split variables."""
    explicit_url = environ.get(DATABASE_URL_ENV_VAR)
    if explicit_url is not None and explicit_url.strip():
        return explicit_url.strip()

    required_parts = {
        DATABASE_HOST_ENV_VAR: environ.get(DATABASE_HOST_ENV_VAR),
        DATABASE_PORT_ENV_VAR: environ.get(DATABASE_PORT_ENV_VAR),
        DATABASE_NAME_ENV_VAR: environ.get(DATABASE_NAME_ENV_VAR),
        DATABASE_USER_ENV_VAR: environ.get(DATABASE_USER_ENV_VAR),
        DATABASE_PASSWORD_ENV_VAR: environ.get(DATABASE_PASSWORD_ENV_VAR),
    }
    if any(value is None or not value.strip() for value in required_parts.values()):
        return None

    driver = environ.get(DATABASE_DRIVER_ENV_VAR, DEFAULT_DATABASE_DRIVER).strip()
    return (
        f"{driver}://{quote(required_parts[DATABASE_USER_ENV_VAR] or '', safe='')}:"
        f"{quote(required_parts[DATABASE_PASSWORD_ENV_VAR] or '', safe='')}"
        f"@{required_parts[DATABASE_HOST_ENV_VAR]}:{required_parts[DATABASE_PORT_ENV_VAR]}"
        f"/{quote(required_parts[DATABASE_NAME_ENV_VAR] or '', safe='')}"
    )


def _build_codex_worker(
    resolved_env: Mapping[str, str], container_manager: DockerSandboxContainerManager
) -> CodexCliWorker:
    codex_runtime_mode = _resolve_default_runtime_mode(
        resolved_env.get(CODEX_RUNTIME_MODE_ENV_VAR),
        default=WorkerRuntimeMode.NATIVE_AGENT,
        worker_name="Codex",
        env_var_name=CODEX_RUNTIME_MODE_ENV_VAR,
    )
    return CodexCliWorker(
        runtime_adapter=CodexExecCliRuntimeAdapter.from_env(resolved_env),
        container_manager=container_manager,
        default_runtime_mode=codex_runtime_mode,
        native_sandbox_mode=resolved_env.get(
            CODEX_SANDBOX_ENV_VAR,
            "workspace-write",
        ),
        native_event_capture_enabled=_is_enabled(
            resolved_env.get(NATIVE_AGENT_EVENT_CAPTURE_ENABLED_ENV_VAR)
        ),
        trusted_repo_patterns=(
            [p.strip() for p in s.split(",") if p.strip()]
            if (s := resolved_env.get(CODEX_TRUSTED_REPO_PATTERNS_ENV_VAR))
            else None
        ),
    )


def _build_gemini_worker(
    resolved_env: Mapping[str, str], container_manager: DockerSandboxContainerManager
) -> GeminiCliWorker | None:
    legacy_gemini_bin = resolved_env.get(GEMINI_EXECUTABLE_ENV_VAR)
    legacy_bin_requests_antigravity = (
        Path(legacy_gemini_bin).name in ANTIGRAVITY_LEGACY_GEMINI_EXECUTABLE_NAMES
        if legacy_gemini_bin
        else False
    )
    antigravity_configured = (
        any(resolved_env.get(k) for k in ANTIGRAVITY_CONFIG_ENV_VARS)
        or legacy_bin_requests_antigravity
    )
    gemini_configured = any(resolved_env.get(k) for k in GEMINI_CONFIG_ENV_VARS)
    if not (antigravity_configured or gemini_configured):
        return None
    if resolved_env.get(ANTIGRAVITY_AUTH_DIR_ENV_VAR):
        logger.info(
            "CODE_AGENT_ANTIGRAVITY_AUTH_DIR is configured for operator guidance only; "
            "Antigravity auth remains keyring-backed and is not copied by the worker."
        )
    gemini_runtime_mode = _resolve_default_runtime_mode(
        resolved_env.get(GEMINI_RUNTIME_MODE_ENV_VAR),
        default=WorkerRuntimeMode.NATIVE_AGENT,
        worker_name="Antigravity" if antigravity_configured else "Gemini",
        env_var_name=GEMINI_RUNTIME_MODE_ENV_VAR,
    )
    runtime_adapter = (
        AntigravityCliRuntimeAdapter.from_env(resolved_env)
        if antigravity_configured
        else GeminiCliRuntimeAdapter.from_env(resolved_env)
    )
    native_sandbox_env_var = (
        ANTIGRAVITY_NATIVE_SANDBOX_ENABLED_ENV_VAR
        if antigravity_configured
        else GEMINI_NATIVE_SANDBOX_ENABLED_ENV_VAR
    )
    native_sandbox_value = resolved_env.get(native_sandbox_env_var)
    if antigravity_configured and native_sandbox_value is None:
        native_sandbox_value = resolved_env.get(GEMINI_NATIVE_SANDBOX_ENABLED_ENV_VAR)
    return GeminiCliWorker(
        runtime_adapter=runtime_adapter,
        container_manager=container_manager,
        default_runtime_mode=gemini_runtime_mode,
        native_sandbox_enabled=_is_enabled(native_sandbox_value),
    )


def _build_openrouter_worker(
    resolved_env: Mapping[str, str], container_manager: DockerSandboxContainerManager
) -> OpenRouterCliWorker | None:
    if resolved_env.get(OPENROUTER_API_KEY_ENV_VAR):
        return OpenRouterCliWorker(
            runtime_adapter=OpenRouterCliRuntimeAdapter.from_env(resolved_env),
            container_manager=container_manager,
        )
    return None


def _build_progress_notifiers(
    resolved_env: Mapping[str, str], outbound_http_clients: OutboundHttpClients
) -> list[ProgressNotifier]:
    progress_notifiers: list[ProgressNotifier] = [
        WebhookCallbackProgressNotifier(client=outbound_http_clients.webhook)
    ]
    telegram_bot_token = resolved_env.get(TELEGRAM_BOT_TOKEN_ENV_VAR)
    if telegram_bot_token:
        progress_notifiers.append(
            TelegramProgressNotifier(
                bot_token=telegram_bot_token,
                client=outbound_http_clients.telegram,
                api_base_url=resolved_env.get(
                    TELEGRAM_API_BASE_URL_ENV_VAR,
                    "https://api.telegram.org",
                ),
            )
        )
    return progress_notifiers


def _build_session_factory(resolved_env: Mapping[str, str]) -> Any:
    database_url = _database_url_from_env(resolved_env)
    if database_url is None:
        raise RuntimeError(
            "Task service bootstrap was enabled, but no database configuration was provided. "
            f"Set {DATABASE_URL_ENV_VAR} or the {DATABASE_HOST_ENV_VAR}/"
            f"{DATABASE_PORT_ENV_VAR}/{DATABASE_NAME_ENV_VAR}/"
            f"{DATABASE_USER_ENV_VAR}/{DATABASE_PASSWORD_ENV_VAR} variables."
        )

    if database_url.startswith("sqlite"):
        engine = create_engine_from_url(
            database_url,
            connect_args={"check_same_thread": False},
        )
    else:
        engine = create_engine_from_url(database_url)
    return create_session_factory(engine)


def _build_container_manager(resolved_env: Mapping[str, str]) -> DockerSandboxContainerManager:
    sandbox_image = resolved_env.get(SANDBOX_IMAGE_ENV_VAR)
    resolved_sandbox_image = sandbox_image.strip() if sandbox_image else ""
    return (
        DockerSandboxContainerManager(default_image=resolved_sandbox_image)
        if resolved_sandbox_image
        else DockerSandboxContainerManager()
    )


def _resolve_workspace_root(resolved_env: Mapping[str, str]) -> Path:
    workspace_root = resolved_env.get(WORKSPACE_ROOT_ENV_VAR)
    if workspace_root is None or not workspace_root.strip():
        return default_workspace_root().resolve()
    return Path(workspace_root).expanduser().resolve()


def _setup_worker_profiles(
    resolved_env: Mapping[str, str],
    gemini_worker: GeminiCliWorker | None,
    openrouter_worker: OpenRouterCliWorker | None,
) -> dict[str, WorkerProfile] | None:
    if not _is_enabled(resolved_env.get(WORKER_PROFILES_ENABLED_ENV_VAR)):
        return None

    codex_legacy_tool_loop_requested = _is_enabled(
        resolved_env.get(CODEX_TOOL_LOOP_LEGACY_ENABLED_ENV_VAR)
    )
    gemini_legacy_tool_loop_requested = gemini_worker is not None and _is_enabled(
        resolved_env.get(GEMINI_TOOL_LOOP_LEGACY_ENABLED_ENV_VAR)
    )
    if codex_legacy_tool_loop_requested:
        logger.warning(
            "Ignoring CODE_AGENT_CODEX_TOOL_LOOP_LEGACY_ENABLED for execution workers; "
            "Codex worker is native-only."
        )
    if gemini_legacy_tool_loop_requested:
        logger.warning(
            "Ignoring CODE_AGENT_GEMINI_TOOL_LOOP_LEGACY_ENABLED for execution workers; "
            "Gemini worker is native-only."
        )
    return _build_default_worker_profiles(
        include_gemini=gemini_worker is not None,
        include_openrouter=(
            openrouter_worker is not None
            and _is_enabled(resolved_env.get(OPENROUTER_ENABLED_ENV_VAR))
        ),
        include_codex_legacy_tool_loop=False,
        include_gemini_legacy_tool_loop=False,
    )


def _build_brain_provider(
    *,
    enable_orchestrator_brain: bool,
    enable_improvement_llm_scoring: bool,
    codex_worker: CodexCliWorker,
    gemini_worker: GeminiCliWorker | None,
    openrouter_worker: OpenRouterCliWorker | None,
) -> RuleBasedOrchestratorBrain | None:
    if not (enable_orchestrator_brain or enable_improvement_llm_scoring):
        return None
    fallback_planners: list[Worker] = [codex_worker]
    if openrouter_worker is not None:
        fallback_planners.append(openrouter_worker)
    return RuleBasedOrchestratorBrain(
        planner_worker=gemini_worker,
        fallback_planners=fallback_planners,
    )


def build_task_service_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    outbound_http_clients: OutboundHttpClients | None = None,
) -> TaskExecutionService | None:
    """Build the real task service when the app is explicitly configured for it."""
    resolved_env = os.environ if environ is None else environ
    if not _is_enabled(resolved_env.get(ENABLE_TASK_SERVICE_ENV_VAR)):
        return None

    session_factory = _build_session_factory(resolved_env)
    container_manager = _build_container_manager(resolved_env)
    resolved_workspace_root = _resolve_workspace_root(resolved_env)

    codex_worker = _build_codex_worker(resolved_env, container_manager)
    gemini_worker = _build_gemini_worker(resolved_env, container_manager)
    openrouter_worker = _build_openrouter_worker(resolved_env, container_manager)
    shell_worker = ShellWorker(
        workspace_root=resolved_workspace_root,
        container_manager=container_manager,
    )

    enable_worker_profiles = _is_enabled(resolved_env.get(WORKER_PROFILES_ENABLED_ENV_VAR))
    worker_profiles = _setup_worker_profiles(resolved_env, gemini_worker, openrouter_worker)
    enable_orchestrator_brain = _is_enabled(resolved_env.get(ORCHESTRATOR_BRAIN_ENABLED_ENV_VAR))
    enable_improvement_llm_scoring = _is_enabled(
        resolved_env.get(IMPROVEMENT_LLM_SCORING_ENABLED_ENV_VAR)
    )
    brain_provider = _build_brain_provider(
        enable_orchestrator_brain=enable_orchestrator_brain,
        enable_improvement_llm_scoring=enable_improvement_llm_scoring,
        codex_worker=codex_worker,
        gemini_worker=gemini_worker,
        openrouter_worker=openrouter_worker,
    )

    if outbound_http_clients is None:
        raise RuntimeError(
            "Task service bootstrap requires shared outbound HTTP clients for notifier delivery."
        )
    progress_notifiers = _build_progress_notifiers(resolved_env, outbound_http_clients)

    return TaskExecutionService(
        session_factory=session_factory,
        worker=codex_worker,
        gemini_worker=gemini_worker,
        openrouter_worker=openrouter_worker,
        shell_worker=shell_worker,
        worker_profiles=worker_profiles,
        enable_worker_profiles=enable_worker_profiles,
        enable_independent_verifier=_is_enabled(
            resolved_env.get(INDEPENDENT_VERIFIER_ENABLED_ENV_VAR)
        ),
        orchestrator_brain=brain_provider if enable_orchestrator_brain else None,
        improvement_scorer=brain_provider if enable_improvement_llm_scoring else None,
        enable_improvement_llm_scoring=enable_improvement_llm_scoring,
        progress_notifier=CompositeProgressNotifier(progress_notifiers),
        default_task_max_attempts=_coerce_positive_int(
            resolved_env.get(DEFAULT_TASK_MAX_ATTEMPTS_ENV_VAR),
            default=3,
        ),
        workspace_root=resolved_workspace_root,
        checkpoint_path=resolved_env.get(CHECKPOINT_DB_PATH_ENV_VAR),
    )
