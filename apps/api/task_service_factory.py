"""Environment-driven task-service bootstrap for the API app."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final
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
    CodexCliWorker,
    CodexExecCliRuntimeAdapter,
    GeminiCliRuntimeAdapter,
    GeminiCliWorker,
    OpenRouterCliRuntimeAdapter,
    OpenRouterCliWorker,
    WorkerProfile,
    WorkerType,
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
CODEX_TOOL_LOOP_LEGACY_ENABLED_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_TOOL_LOOP_LEGACY_ENABLED"
GEMINI_TOOL_LOOP_LEGACY_ENABLED_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_TOOL_LOOP_LEGACY_ENABLED"
CODEX_TRUSTED_REPO_PATTERNS_ENV_VAR: Final[str] = "CODE_AGENT_CODEX_TRUSTED_REPO_PATTERNS"
GEMINI_NATIVE_SANDBOX_ENABLED_ENV_VAR: Final[str] = "CODE_AGENT_GEMINI_NATIVE_SANDBOX_ENABLED"

# Default profile names
GEMINI_NATIVE_PLANNER_PROFILE: Final[str] = "gemini-native-planner"
GEMINI_NATIVE_REVIEWER_PROFILE: Final[str] = "gemini-native-reviewer"

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


def _build_default_worker_profiles(
    *,
    include_gemini: bool,
    include_openrouter: bool,
    include_codex_legacy_tool_loop: bool,
    include_gemini_legacy_tool_loop: bool,
) -> dict[str, WorkerProfile]:
    """Build the default executable profile map for routing decisions."""
    profiles: dict[str, WorkerProfile] = {}

    def _add_profiles(
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

    _add_profiles("codex", WorkerRuntimeMode.NATIVE_AGENT)
    if include_codex_legacy_tool_loop:
        _add_profiles("codex", WorkerRuntimeMode.TOOL_LOOP, legacy_mode=True)

    if include_gemini:
        _add_profiles("gemini", WorkerRuntimeMode.NATIVE_AGENT)
        if include_gemini_legacy_tool_loop:
            _add_profiles("gemini", WorkerRuntimeMode.TOOL_LOOP, legacy_mode=True)
        # Add specialized profiles for Gemini (T-142)
        profiles[GEMINI_NATIVE_PLANNER_PROFILE] = WorkerProfile(
            name=GEMINI_NATIVE_PLANNER_PROFILE,
            worker_type="gemini",
            runtime_mode=WorkerRuntimeMode.PLANNER_ONLY,
            capability_tags=["planning"],
            supported_delivery_modes=["workspace", "branch", "draft_pr"],
            permission_profile="workspace_write",
            mutation_policy="patch_allowed",
            self_review_policy="on_failure",
        )
        profiles[GEMINI_NATIVE_REVIEWER_PROFILE] = WorkerProfile(
            name=GEMINI_NATIVE_REVIEWER_PROFILE,
            worker_type="gemini",
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


def build_task_service_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    outbound_http_clients: OutboundHttpClients | None = None,
) -> TaskExecutionService | None:
    """Build the real task service when the app is explicitly configured for it."""
    resolved_env = os.environ if environ is None else environ
    if not _is_enabled(resolved_env.get(ENABLE_TASK_SERVICE_ENV_VAR)):
        return None

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
    session_factory = create_session_factory(engine)
    sandbox_image = resolved_env.get(SANDBOX_IMAGE_ENV_VAR)
    resolved_sandbox_image = sandbox_image.strip() if sandbox_image else ""
    container_manager = (
        DockerSandboxContainerManager(default_image=resolved_sandbox_image)
        if resolved_sandbox_image
        else DockerSandboxContainerManager()
    )
    codex_runtime_mode = _resolve_default_runtime_mode(
        resolved_env.get(CODEX_RUNTIME_MODE_ENV_VAR),
        default=WorkerRuntimeMode.NATIVE_AGENT,
        worker_name="Codex",
        env_var_name=CODEX_RUNTIME_MODE_ENV_VAR,
    )
    gemini_runtime_mode = _resolve_default_runtime_mode(
        resolved_env.get(GEMINI_RUNTIME_MODE_ENV_VAR),
        default=WorkerRuntimeMode.NATIVE_AGENT,
        worker_name="Gemini",
        env_var_name=GEMINI_RUNTIME_MODE_ENV_VAR,
    )

    codex_worker = CodexCliWorker(
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
    gemini_worker: GeminiCliWorker | None = None
    openrouter_worker: OpenRouterCliWorker | None = None
    if any(
        resolved_env.get(k)
        for k in (GEMINI_EXECUTABLE_ENV_VAR, GEMINI_MODEL_ENV_VAR, GEMINI_TIMEOUT_ENV_VAR)
    ):
        gemini_worker = GeminiCliWorker(
            runtime_adapter=GeminiCliRuntimeAdapter.from_env(resolved_env),
            container_manager=container_manager,
            default_runtime_mode=gemini_runtime_mode,
            native_sandbox_enabled=_is_enabled(
                resolved_env.get(GEMINI_NATIVE_SANDBOX_ENABLED_ENV_VAR)
            ),
        )
    if resolved_env.get(OPENROUTER_API_KEY_ENV_VAR):
        openrouter_worker = OpenRouterCliWorker(
            runtime_adapter=OpenRouterCliRuntimeAdapter.from_env(resolved_env),
            container_manager=container_manager,
        )
    enable_worker_profiles = _is_enabled(resolved_env.get(WORKER_PROFILES_ENABLED_ENV_VAR))
    worker_profiles: dict[str, WorkerProfile] | None = None
    if enable_worker_profiles:
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
        worker_profiles = _build_default_worker_profiles(
            include_gemini=gemini_worker is not None,
            include_openrouter=(
                openrouter_worker is not None
                and _is_enabled(resolved_env.get(OPENROUTER_ENABLED_ENV_VAR))
            ),
            include_codex_legacy_tool_loop=False,
            include_gemini_legacy_tool_loop=False,
        )
    if outbound_http_clients is None:
        raise RuntimeError(
            "Task service bootstrap requires shared outbound HTTP clients for notifier delivery."
        )
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
    workspace_root = resolved_env.get(WORKSPACE_ROOT_ENV_VAR)
    if workspace_root is None or not workspace_root.strip():
        resolved_workspace_root = default_workspace_root().resolve()
    else:
        resolved_workspace_root = Path(workspace_root).expanduser().resolve()
    return TaskExecutionService(
        session_factory=session_factory,
        worker=codex_worker,
        gemini_worker=gemini_worker,
        openrouter_worker=openrouter_worker,
        worker_profiles=worker_profiles,
        enable_worker_profiles=enable_worker_profiles,
        enable_independent_verifier=_is_enabled(
            resolved_env.get(INDEPENDENT_VERIFIER_ENABLED_ENV_VAR)
        ),
        orchestrator_brain=(
            RuleBasedOrchestratorBrain(planner_worker=gemini_worker)
            if _is_enabled(resolved_env.get(ORCHESTRATOR_BRAIN_ENABLED_ENV_VAR))
            else None
        ),
        progress_notifier=CompositeProgressNotifier(progress_notifiers),
        default_task_max_attempts=_coerce_positive_int(
            resolved_env.get(DEFAULT_TASK_MAX_ATTEMPTS_ENV_VAR),
            default=3,
        ),
        workspace_root=resolved_workspace_root,
        checkpoint_path=resolved_env.get(CHECKPOINT_DB_PATH_ENV_VAR),
    )
