"""Background scheduler for CI polling and repair tasks."""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from apps.api.config import SystemConfig
from db.enums import WorkerRunStatus
from db.models import WorkerRun
from orchestrator.execution import TaskExecutionService
from orchestrator.execution_types import DeliveryKey, SubmissionSession, TaskSubmission
from orchestrator.github_repo import github_repo_spec_from_url
from repositories import session_scope

logger = logging.getLogger(__name__)


class CIPollingScheduler:
    """Polls CI checks for delivered tasks and spawns repair tasks on failure."""

    def __init__(self, task_service: TaskExecutionService, config: SystemConfig) -> None:
        self.task_service = task_service
        self.config = config
        self.session_factory: Any | None = getattr(task_service, "session_factory", None)
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._loop_ref: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Start the background scheduler loop."""
        if self._running:
            return
        if not self.config.ci_polling_enabled:
            logger.info("CIPollingScheduler is disabled via config.")
            return
        if shutil.which("gh") is None:
            logger.warning("CIPollingScheduler enabled but gh CLI was not found on PATH.")
        if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
            logger.warning("CIPollingScheduler enabled but GH_TOKEN/GITHUB_TOKEN is not set.")
        if self.session_factory is None:
            logger.warning("CIPollingScheduler enabled but task service has no session factory.")
            return

        try:
            self._loop_ref = asyncio.get_running_loop()
        except RuntimeError:
            self._loop_ref = None

        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("CIPollingScheduler started.")

    async def stop(self) -> None:
        """Stop the background scheduler loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("CIPollingScheduler stopped.")

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in CIPollingScheduler tick: {e}", exc_info=True)

            try:
                interval = max(60, self.config.ci_polling_interval_minutes * 60)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    def tick(self) -> None:
        """Evaluate pending CI checks and synchronously submit repair tasks if needed."""
        if not self.config.ci_polling_enabled:
            return

        pending_runs = self._get_pending_runs()
        for run in pending_runs:
            self._poll_run(run)

    def _get_pending_runs(self) -> list[dict[str, Any]]:
        # Fetch runs from last 7 days that might have active PRs
        session_factory = self.session_factory
        if session_factory is None:
            return []
        cutoff = datetime.now(UTC) - timedelta(days=7)
        results = []
        with session_scope(session_factory) as session:
            stmt = (
                select(WorkerRun)
                .options(selectinload(WorkerRun.task))
                .where(
                    WorkerRun.status == WorkerRunStatus.SUCCESS,
                    WorkerRun.started_at >= cutoff,
                )
            )
            for run in session.scalars(stmt):
                if not run.delivery_metadata:
                    continue

                ci_status = run.delivery_metadata.get("ci_status")
                # Only poll if not already finalized
                if ci_status in ("passed", "success"):
                    continue

                results.append(
                    {
                        "run_id": run.id,
                        "task_id": run.task_id,
                        "repo_url": run.task.repo_url if run.task else None,
                        "delivery_metadata": run.delivery_metadata,
                    }
                )
        return results

    def _poll_run(self, run_info: dict[str, Any]) -> None:
        metadata = run_info["delivery_metadata"]
        branch_name = metadata.get("branch_name")
        repo_url = run_info.get("repo_url")
        head_sha = metadata.get("head_sha")

        if not branch_name or not repo_url or not head_sha:
            return
        repo_spec = github_repo_spec_from_url(repo_url)
        if repo_spec is None:
            logger.warning(
                "CIPollingScheduler: unable to derive GitHub repo spec",
                extra={"task_id": run_info["task_id"], "repo_url": repo_url},
            )
            return

        gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not gh_token:
            logger.warning("CIPollingScheduler: GH_TOKEN missing")
            return

        env = os.environ.copy()
        env["GH_TOKEN"] = gh_token

        # Check PR checks via gh cli
        cmd = [
            "gh",
            "pr",
            "checks",
            branch_name,
            "-R",
            repo_spec,
            "--json",
            "name,state,link",
        ]
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if proc.returncode != 0:
                logger.debug(f"Failed to check PR checks for {branch_name}")
                return
            checks = json.loads(proc.stdout)
        except Exception as e:
            logger.debug(f"Error checking PR: {e}")
            return

        if not isinstance(checks, list):
            logger.debug(f"Unexpected response format from gh pr checks: {type(checks)}")
            return

        all_passed = True
        failed_checks = []
        for check in checks:
            if not isinstance(check, dict):
                continue
            state = check.get("state", "").upper()
            if state in ("FAILURE", "STARTUP_FAILURE", "ERROR"):
                failed_checks.append(check)
                all_passed = False
            elif state in ("PENDING", "IN_PROGRESS", "QUEUED"):
                all_passed = False

        new_status = "failed" if failed_checks else "passed" if all_passed else "pending"
        self._update_run_ci_metadata(run_info["run_id"], new_status, failed_checks)

        if failed_checks:
            for check in failed_checks:
                self._submit_repair_task(
                    run_info["task_id"],
                    repo_url,
                    repo_spec,
                    branch_name,
                    head_sha,
                    check,
                    env,
                )

    def _update_run_ci_metadata(
        self,
        run_id: str,
        new_status: str,
        failed_checks: list[dict[str, Any]],
    ) -> None:
        session_factory = self.session_factory
        if session_factory is None:
            return
        with session_scope(session_factory) as session:
            db_run = session.get(WorkerRun, run_id)
            if not db_run or not db_run.delivery_metadata:
                return
            db_run.delivery_metadata["ci_status"] = new_status
            db_run.delivery_metadata["ci_last_checked_at"] = datetime.now(UTC).isoformat()
            if new_status == "failed":
                db_run.delivery_metadata["ci_failed_jobs"] = [
                    name for c in failed_checks if isinstance((name := c.get("name")), str)
                ]
            flag_modified(db_run, "delivery_metadata")

    def _submit_repair_task(
        self,
        task_id: str,
        repo_url: str,
        repo_spec: str,
        branch_name: str,
        head_sha: str,
        check: dict[str, Any],
        env: dict[str, str],
    ) -> None:
        check_name = str(check.get("name") or "unknown-check")
        delivery_key = DeliveryKey(
            channel="ci_polling",
            delivery_id=f"ci_repair:{task_id}:{head_sha}:{check_name}",
        )

        logs = self._fetch_logs(repo_spec, head_sha, check, env)
        if logs:
            try:
                if self._loop_ref and self._loop_ref.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self._parse_logs_with_llm_async(logs, check_name), self._loop_ref
                    )
                    parsed_logs = future.result()
                else:
                    parsed_logs = asyncio.run(self._parse_logs_with_llm_async(logs, check_name))
            except Exception as e:
                logger.warning(f"Failed to run async LLM parsing: {e}")
                parsed_logs = logs
        else:
            parsed_logs = None

        task_text = f"The CI check '{check_name}' failed for branch '{branch_name}'.\n"
        task_text += f"Link: {check.get('link')}\n\n"
        if parsed_logs:
            task_text += f"Error Summary / Logs:\n```\n{parsed_logs}\n```\n"
        else:
            task_text += "Logs could not be fetched. Please review the link.\n"

        task_text += "\nPlease investigate and fix the issue. Commit and push the fix."

        submission = TaskSubmission(
            task_text=task_text,
            repo_url=repo_url,
            branch=branch_name,
            priority=10,
            repair_for_task_id=task_id,
            session=SubmissionSession(
                channel="ci_polling",
                external_user_id="system:ci-polling",
                external_thread_id=task_id,
                display_name="CI Repair",
            ),
        )

        try:
            outcome = self.task_service.create_task_outcome(submission, delivery_key=delivery_key)
            if not outcome.duplicate:
                logger.info(f"Spawned CI repair task {outcome.task_snapshot.task_id} for {task_id}")
        except Exception as e:
            logger.error(f"Failed to spawn repair task: {e}")

    async def _parse_logs_with_llm_async(self, logs: str, check_name: str) -> str:
        if (
            self.config.ci_polling_llm_profile
            and self.config.ci_polling_llm_profile.lower() == "none"
        ):
            return logs

        worker = self.task_service.gemini_worker or self.task_service.worker
        if not worker or not logs:
            return logs

        from workers.base import WorkerRequest

        prompt = (
            f"You are a CI log analyzer. The check '{check_name}' failed. "
            "Please read the following logs and extract the exact error or test failure, "
            "providing a concise summary of what needs to be fixed. Do NOT suggest code, "
            "just summarize the failure. If you cannot find the error, return the logs as is.\n\n"
            f"Logs:\n{logs}"
        )
        kwargs: dict[str, Any] = {"task_text": prompt}
        if self.config.ci_polling_llm_profile:
            kwargs["worker_profile"] = self.config.ci_polling_llm_profile

        request = WorkerRequest(**kwargs)
        try:
            result = await worker.run(request)
            if result is None:
                logger.warning("LLM parsing returned None result")
                return logs
            if result.status == "success" and result.summary:
                return result.summary
        except Exception as e:
            logger.warning(f"LLM parsing failed: {e}")
        return logs

    def _fetch_logs(
        self,
        repo_spec: str,
        head_sha: str,
        check: dict[str, Any],
        env: dict[str, str],
    ) -> str | None:
        link = check.get("link", "")
        run_id = None
        match = re.search(r"/runs/(\d+)", link)
        if match:
            run_id = match.group(1)
        else:
            cmd = [
                "gh",
                "run",
                "list",
                "--commit",
                head_sha,
                "-R",
                repo_spec,
                "--json",
                "databaseId,workflowName,status,conclusion",
            ]
            try:
                proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
                if proc.returncode == 0:
                    runs = json.loads(proc.stdout)
                    for r in runs:
                        if r.get("conclusion") == "failure" and r.get("workflowName") == check.get(
                            "name"
                        ):
                            run_id = str(r.get("databaseId"))
                            break
                    if not run_id and runs:
                        for r in runs:
                            if r.get("conclusion") == "failure":
                                run_id = str(r.get("databaseId"))
                                break
            except Exception:
                pass

        if not run_id:
            return None

        cmd_log = ["gh", "run", "view", run_id, "--log-failed", "-R", repo_spec]
        try:
            proc = subprocess.run(cmd_log, env=env, capture_output=True, text=True)
            if proc.returncode == 0:
                log_data = proc.stdout
                limit = self.config.ci_polling_log_limit_bytes
                if limit <= 0:
                    return ""
                if len(log_data) > limit:
                    return log_data[-limit:]
                return log_data
        except Exception:
            pass

        return None
