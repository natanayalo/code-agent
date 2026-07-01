"""Unified WorkerFacade serving as the entry point for worker adapters."""

from __future__ import annotations

import logging

from workers.base import Worker, WorkerRequest, WorkerResult, WorkerType, normalize_worker_type

logger = logging.getLogger(__name__)


class WorkerFacade(Worker):
    """Facade for routing to concrete worker adapters based on worker_type."""

    def __init__(
        self,
        *,
        codex_worker: Worker | None = None,
        antigravity_worker: Worker | None = None,
        openrouter_worker: Worker | None = None,
        shell_worker: Worker | None = None,
    ) -> None:
        self._workers: dict[str, Worker] = {}

        if codex_worker is not None:
            self._workers["codex"] = codex_worker
        if antigravity_worker is not None:
            self._workers["antigravity"] = antigravity_worker
        if openrouter_worker is not None:
            self._workers["openrouter"] = openrouter_worker

        self._shell_worker = shell_worker

    def available_workers(self) -> dict[str, Worker]:
        """Return all registered workers including the shell support worker."""
        workers = dict(self._workers)
        if self._shell_worker is not None:
            workers["shell"] = self._shell_worker
        return workers

    def get_worker(self, worker_type: WorkerType | str) -> Worker | None:
        """Look up a canonical worker by type."""
        return self._workers.get(normalize_worker_type(worker_type))

    def get_shell_worker(self) -> Worker | None:
        """Get the specialized shell support worker if configured."""
        return self._shell_worker

    async def run(
        self,
        request: WorkerRequest,
        *,
        system_prompt: str | None = None,
    ) -> WorkerResult:
        """Route the task to the selected worker based on the request."""
        worker_type = request.worker_type
        if not worker_type and request.runtime_manifest:
            worker = request.runtime_manifest.get("worker")
            if isinstance(worker, dict):
                worker_type = worker.get("worker_type")

        if not worker_type:
            return WorkerResult(
                status="error",
                summary="WorkerFacade received a request with no worker_type specified.",
                failure_kind="provider_error",
                next_action_hint="configure_requested_worker",
                commands_run=[],
                files_changed=[],
                test_results=[],
                artifacts=[],
            )

        # Optional validation that the explicit type matches the manifest
        if (
            request.worker_type
            and request.runtime_manifest
            and isinstance(request.runtime_manifest.get("worker"), dict)
            and "worker_type" in request.runtime_manifest["worker"]
        ):
            manifest_type = normalize_worker_type(request.runtime_manifest["worker"]["worker_type"])
            if normalize_worker_type(request.worker_type) != manifest_type:
                logger.error(
                    "WorkerRequest worker_type %s conflicts with runtime_manifest worker_type %s",
                    request.worker_type,
                    manifest_type,
                )
                return WorkerResult(
                    status="error",
                    summary=(
                        f"WorkerFacade contract error: explicit worker_type '{request.worker_type}'"
                        f" disagrees with runtime_manifest worker_type '{manifest_type}'."
                    ),
                    failure_kind="provider_error",
                    next_action_hint="configure_requested_worker",
                    commands_run=[],
                    files_changed=[],
                    test_results=[],
                    artifacts=[],
                )

        concrete_worker = self.get_worker(worker_type)
        if concrete_worker is None:
            available_types = ", ".join(sorted(self._workers.keys())) or "none"
            return WorkerResult(
                status="failure",
                summary=(
                    f"No worker is available for route '{worker_type}'. "
                    f"Available workers: {available_types}."
                ),
                failure_kind="provider_error",
                next_action_hint="configure_requested_worker",
                commands_run=[],
                files_changed=[],
                test_results=[],
                artifacts=[],
            )

        return await concrete_worker.run(request, system_prompt=system_prompt)
