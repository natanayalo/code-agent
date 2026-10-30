"""Sanitized worker prerequisite observations and alert identities."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from db.base import utc_now
from db.enums import WorkerNodeStatus
from db.models import WorkerNode
from orchestrator.execution_worker_service import _profile_worker_type
from orchestrator.provider_diagnostics import ProviderDiagnosticsService
from repositories import session_scope

logger = logging.getLogger(__name__)
PROVIDER_REASON_PREFIX = "provider|"
PROVIDERS = frozenset({"codex", "antigravity", "openrouter"})
ACTIONS = {
    "credentials": "Check the configured provider credentials on the worker.",
    "docker_daemon": "Check Docker availability on the worker.",
    "executor_image": "Check the local executor image on the worker.",
    "cli_binary": "Check the provider executable on the worker.",
}
SCOPES = frozenset({"local_presence", "local_structure", "local_runtime", "remote_validity"})


def alert_identity(reason: str) -> tuple[str, str, str] | None:
    parts = reason.split("|")
    if len(parts) != 4 or parts[0] != "provider":
        return None
    _, worker_id, provider, capability = parts
    try:
        if str(UUID(worker_id)) != worker_id:
            return None
    except ValueError:
        return None
    if provider not in PROVIDERS or capability not in ACTIONS:
        return None
    return worker_id, provider, capability


def selected_providers(service: Any) -> set[str]:
    """Intersect concrete adapters and enabled execution profiles."""
    available: dict[str, Any] = getattr(service.worker, "available_workers", lambda: {})()
    if not getattr(service, "enable_worker_profiles", False):
        return set()
    profiles = getattr(service, "worker_profiles", {}) or {}
    configured = set()
    for name, profile in profiles.items():
        try:
            configured.add(_profile_worker_type(name, profile).value)
        except ValueError:
            logger.warning("Skipping invalid provider observation profile")
    return set(available) & configured & PROVIDERS


async def observe_providers(service: Any) -> list[dict[str, str]]:
    workers = service.worker.available_workers()
    diagnostics = ProviderDiagnosticsService(workers=workers, worker=service.worker)

    async def probe(provider: str) -> list[dict[str, str]]:
        try:
            context, _ = diagnostics._operator_context(provider)
        except Exception as exc:
            logger.warning(
                "Provider context failed provider=%s error=%s", provider, type(exc).__name__
            )
            return []

        async def bounded(awaitable: Any) -> Any:
            try:
                return await asyncio.wait_for(awaitable, timeout=diagnostics.preflight_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Provider check failed provider=%s error=%s", provider, type(exc).__name__
                )
                return None

        async def container_checks() -> list[Any]:
            if not context.is_container_execution:
                return []
            docker = await bounded(diagnostics.check_docker_daemon())
            checks = [docker]
            if docker and docker.status == "ready" and context.executor_image:
                checks.append(
                    await bounded(diagnostics.check_executor_image(context.executor_image))
                )
            return checks

        credentials, cli, container = await asyncio.gather(
            bounded(asyncio.to_thread(diagnostics.check_credentials, context)),
            bounded(asyncio.to_thread(diagnostics.check_cli_binary, context)),
            container_checks(),
        )
        observed_at = utc_now().isoformat()
        samples = []
        for check in [credentials, cli, *container]:
            if check is None or check.name not in ACTIONS:
                continue
            outcome = check.status if check.probe_result == "completed" else "unknown"
            samples.append(
                {
                    "provider": provider,
                    "check": check.name,
                    "outcome": outcome,
                    "verification_scope": check.verification_scope,
                    "observed_at": observed_at,
                }
            )
        return samples

    batches = await asyncio.gather(
        *(probe(provider) for provider in sorted(selected_providers(service)))
    )
    return [sample for batch in batches for sample in batch]


def save_observations(
    service: Any, worker_id: str, process_identity: str, observations: list[dict[str, str]]
) -> bool:
    with session_scope(service.session_factory) as session:
        node = session.scalar(
            select(WorkerNode).where(WorkerNode.worker_id == worker_id).with_for_update()
        )
        if node is None or node.process_identity != process_identity:
            return False
        capabilities = dict(node.capabilities or {})
        capabilities["provider_observations"] = {"version": 1, "samples": observations}
        node.capabilities = capabilities
        return True


async def observation_loop(service: Any, worker_id: str, process_identity: str) -> None:
    while True:
        started = asyncio.get_running_loop().time()
        try:
            samples = await observe_providers(service)
            saved = await asyncio.to_thread(
                save_observations, service, worker_id, process_identity, samples
            )
            if not saved:
                logger.warning("Provider observations rejected for replaced worker")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Provider observation iteration failed error=%s", type(exc).__name__)
        await asyncio.sleep(max(0, 30 - (asyncio.get_running_loop().time() - started)))


def fresh_observations(session: Any, now: datetime) -> list[tuple[str, bool, datetime]]:
    """Return only fresh, valid, conclusive samples from active worker registrations."""
    result: list[tuple[str, bool, datetime]] = []
    for node in session.scalars(
        select(WorkerNode).where(WorkerNode.status == WorkerNodeStatus.ACTIVE).with_for_update()
    ):
        data = (node.capabilities or {}).get("provider_observations", {})
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("samples"), list)
        ):
            continue
        for sample in data["samples"]:
            if not isinstance(sample, dict):
                continue
            provider, capability = sample.get("provider"), sample.get("check")
            reason = f"{PROVIDER_REASON_PREFIX}{node.id}|{provider}|{capability}"
            if (
                alert_identity(reason) is None
                or sample.get("outcome") not in {"ready", "unready"}
                or sample.get("verification_scope") not in SCOPES
            ):
                continue
            try:
                observed_at = datetime.fromisoformat(sample["observed_at"])
                if (
                    observed_at.tzinfo is None
                    or not now - timedelta(seconds=90) <= observed_at <= now
                ):
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            result.append((reason, sample["outcome"] == "unready", observed_at))
    return result
