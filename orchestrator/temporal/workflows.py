from __future__ import annotations

import asyncio
from typing import Any

from temporalio import workflow
from temporalio.exceptions import CancelledError as TemporalCancelledError

from orchestrator.temporal.policy import activity_options

ACCEPTANCE_PATCH_ID = "task-delivery-acceptance-v1"
MAX_PERMISSION_ESCALATIONS = 5
NODE_WAVE_PATCH_ID = "m25-1b-temporal-node-wave"
M25_2_FANOUT_PATCH_ID = "m25-2-bounded-selective-fanout"
COMPLETION_LOOP_PATCH_ID = "m25-4-temporal-completion-loop"
CANCELLATION_TERMINAL_PATCH_ID = "m25-6-cancelled-terminal-state"
REJECTED_SESSION_STATE_PATCH_ID = "m28-2-rejected-session-state"


def _temporal_cancellation_cause(exc: Exception) -> TemporalCancelledError | None:
    """Return a cancellation wrapped by an Activity failure, if present."""
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, TemporalCancelledError):
            return cause
        cause = getattr(cause, "cause", None) or cause.__cause__
    return None


@workflow.defn
class TaskExecutionWorkflow:
    def __init__(self) -> None:
        self.approval_decision: bool | None = None
        self.clarification_resolved = False
        self.permission_escalation_decision: bool | None = None
        self.processed_command_keys: set[str] = set()

        self.task_id: str | None = None
        self.lifecycle_stage: str = "initializing"
        self.escalation_count: int = 0
        self.completion_continuation: str | None = None
        self.current_plan: dict[str, Any] = {}
        self.pending_decision: dict[str, Any] | None = None

    @workflow.run
    async def run(self, task_id: str) -> dict:
        cancellation_terminal_enabled = workflow.patched(CANCELLATION_TERMINAL_PATCH_ID)
        try:
            return await self._run_lifecycle(task_id)
        except Exception as exc:
            cancellation = _temporal_cancellation_cause(exc)
            if cancellation_terminal_enabled and cancellation is not None:
                self.lifecycle_stage = "cancelled"
                raise cancellation
            self.lifecycle_stage = "failed"
            await workflow.execute_activity(
                "record_workflow_failure",
                args=[task_id, str(exc)],
                **activity_options("record_workflow_failure"),
            )
            return {"status": "failed", "summary": "Temporal workflow activity failed."}

    async def _run_lifecycle(self, task_id: str) -> dict:
        self.task_id = task_id
        self.lifecycle_stage = "planning"
        rejected, queue = await self._plan_and_check_initial_gates(task_id)
        if rejected is not None:
            return rejected

        preflight_failure, shape = await self._prepare_execution(task_id)
        if preflight_failure is not None:
            return preflight_failure

        use_node_waves = workflow.patched(NODE_WAVE_PATCH_ID) and (shape == "decomposed")
        self.current_plan["use_node_waves"] = use_node_waves
        escalation_failure = (
            await self._run_decomposed_node_waves(task_id)
            if use_node_waves
            else await self._run_worker_with_permission_escalations(task_id, queue)
        )
        if escalation_failure is not None:
            self.lifecycle_stage = escalation_failure.get("status", "failed")
            return escalation_failure

        self.lifecycle_stage = "verifying"
        completion_decision: dict[str, Any] = {
            "continuation": "complete",
            "summary": "verification and review completed",
        }
        if workflow.patched(COMPLETION_LOOP_PATCH_ID):
            completion_failure, completion_decision = await self._run_completion_loop(
                task_id, queue
            )
            if completion_failure is not None:
                self.lifecycle_stage = completion_failure.get("status", "failed")
                return completion_failure
        else:
            await workflow.execute_activity(
                "verify_result", task_id, **activity_options("verify_result")
            )

        self.completion_continuation = completion_decision.get("continuation")
        self.current_plan["last_continuation"] = self.completion_continuation
        self.lifecycle_stage = "delivering"
        final_result = await self._persist_and_deliver(task_id, completion_decision)
        self.lifecycle_stage = final_result.get("status", "completed")
        return final_result

    async def _plan_and_check_initial_gates(self, task_id: str) -> tuple[dict | None, str | None]:
        res = await workflow.execute_activity(
            "classify_and_plan", task_id, **activity_options("classify_and_plan")
        )
        queue = res.get("execution_task_queue")
        self.current_plan = {"execution_task_queue": queue}
        if res.get("requires_clarification", False):
            self.lifecycle_stage = "awaiting_clarification"
            self.pending_decision = {
                "kind": "clarification",
                "node_id": None,
                "escalation_count": self.escalation_count,
            }
            try:
                await workflow.wait_condition(lambda: self.clarification_resolved)
            finally:
                self.pending_decision = None

        if res.get("requires_approval", False) and not await self._await_initial_approval(task_id):
            self.lifecycle_stage = "rejected"
            return {"status": "rejected", "summary": "Manual approval rejected."}, queue
        return None, queue

    async def _prepare_execution(self, task_id: str) -> tuple[dict | None, str | None]:
        self.lifecycle_stage = "decomposing"
        decomposition = await workflow.execute_activity(
            "decompose_task", task_id, **activity_options("decompose_task")
        )
        shape = (decomposition or {}).get("execution_shape")
        self.current_plan["execution_shape"] = shape
        if shape == "preflight_failed":
            self.lifecycle_stage = "preflight_failed"
            failure = await self._record_post_terminal_preflight_failure(task_id, decomposition)
            return failure, shape

        self.lifecycle_stage = "loading_memory"
        await workflow.execute_activity("load_memory", task_id, **activity_options("load_memory"))
        self.lifecycle_stage = "provisioning_workspace"
        await workflow.execute_activity(
            "provision_workspace", task_id, **activity_options("provision_workspace")
        )
        return None, shape

    async def _record_post_terminal_preflight_failure(
        self,
        task_id: str,
        decomposition: dict[str, Any],
    ) -> dict[str, str]:
        """Persist the typed preflight reason without loading memory or starting execution."""
        failure = decomposition.get("preflight_failure") or (
            "Post-terminal quality evaluation preflight failed."
        )
        await workflow.execute_activity(
            "record_workflow_failure",
            args=[task_id, failure],
            **activity_options("record_workflow_failure"),
        )
        return {"status": "failed", "summary": failure}

    async def _await_initial_approval(self, task_id: str) -> bool:
        """Wait for approval and retain typed state when the operator rejects it."""
        self.lifecycle_stage = "awaiting_approval"
        self.pending_decision = {
            "kind": "approval",
            "node_id": None,
            "escalation_count": self.escalation_count,
        }
        try:
            await workflow.wait_condition(lambda: self.approval_decision is not None)
            if self.approval_decision:
                return True
            if workflow.patched(REJECTED_SESSION_STATE_PATCH_ID):
                await workflow.execute_activity(
                    "persist_rejected_session_state",
                    task_id,
                    **activity_options("persist_rejected_session_state"),
                )
            return False
        finally:
            self.pending_decision = None

    async def _persist_and_deliver(
        self,
        task_id: str,
        completion_decision: dict[str, Any],
    ) -> dict:
        """Persist the final snapshot and project exactly one terminal delivery."""
        await workflow.execute_activity(
            "persist_memory", task_id, **activity_options("persist_memory")
        )
        acceptance_enabled = workflow.patched(ACCEPTANCE_PATCH_ID)
        delivery_outcome = await workflow.execute_activity(
            "deliver_result", task_id, **activity_options("deliver_result")
        )

        if completion_decision.get("continuation") == "manual_follow_up":
            return {
                "status": "failed",
                "summary": completion_decision.get("summary") or "Manual follow-up is required.",
            }
        if (
            acceptance_enabled
            and isinstance(delivery_outcome, dict)
            and delivery_outcome.get("status") == "failed"
        ):
            return {"status": "failed", "summary": "Task acceptance or delivery failed."}

        return {"status": "completed", "summary": "Task completed successfully via Temporal."}

    async def _run_completion_loop(
        self,
        task_id: str,
        execution_task_queue: str | None,
    ) -> tuple[dict | None, dict[str, Any]]:
        """Repeat worker execution only when verification returns a durable repair decision."""
        while True:
            raw_decision = await workflow.execute_activity(
                "verify_result", task_id, **activity_options("verify_result")
            )
            decision = raw_decision if isinstance(raw_decision, dict) else {}
            continuation = decision.get("continuation", "complete")
            if continuation in {"complete", "manual_follow_up"}:
                return None, decision
            if continuation != "repair":
                failure = "Verification returned an unknown completion-loop continuation."
                await workflow.execute_activity(
                    "record_workflow_failure",
                    args=[task_id, failure],
                    **activity_options("record_workflow_failure"),
                )
                return {"status": "failed", "summary": failure}, decision

            await workflow.execute_activity(
                "provision_workspace", task_id, **activity_options("provision_workspace")
            )
            escalation_failure = await self._run_worker_with_permission_escalations(
                task_id,
                execution_task_queue,
            )
            if escalation_failure is not None:
                if escalation_failure.get("status") == "rejected":
                    return None, {
                        "continuation": "manual_follow_up",
                        "summary": (
                            "Repair permission was rejected by the operator; "
                            "manual follow-up is required."
                        ),
                    }
                return escalation_failure, decision

    async def _run_decomposed_node_waves(self, task_id: str) -> dict | None:
        """Coordinate exactly one durable node execution before every merge."""
        escalation_count = 0
        fanout_contract_enabled = workflow.patched(M25_2_FANOUT_PATCH_ID)
        while True:
            if fanout_contract_enabled:
                selection = await workflow.execute_activity(
                    "select_next_node_v2", task_id, **activity_options("select_next_node_v2")
                )
            else:
                selection = await workflow.execute_activity(
                    "select_next_node", task_id, **activity_options("select_next_node")
                )
            if (
                fanout_contract_enabled
                and selection.get("schema_version") == 2
                and selection.get("fanout_applied")
            ):
                items = selection.get("items") or []
                self.current_plan["active_node_ids"] = [
                    item.get("node_id") for item in items if item.get("node_id")
                ]
                self.current_plan["last_wave_action"] = "fanout"
                merge = await self._run_fanout_wave(task_id, selection)
            else:
                action = selection.get("action")
                self.current_plan["last_wave_action"] = action
                node_id = selection.get("node_id")
                self.current_plan["active_node_ids"] = [node_id] if node_id else []
                if action not in {
                    "execute",
                    "merge_terminal",
                    "skip",
                    "await_permission",
                    "complete",
                    "invalid",
                }:
                    failure = "Node selection returned an unknown coordinator action."
                    await workflow.execute_activity(
                        "record_workflow_failure",
                        args=[task_id, failure],
                        **activity_options("record_workflow_failure"),
                    )
                    return {"status": "failed", "summary": failure}
                if action == "complete":
                    return None
                if action == "invalid":
                    failure = selection.get("reason") or "Invalid decomposed execution plan."
                    await workflow.execute_activity(
                        "record_workflow_failure",
                        args=[task_id, failure],
                        **activity_options("record_workflow_failure"),
                    )
                    return {"status": "failed", "summary": failure}
                if action == "await_permission":
                    self.escalation_count = escalation_count + 1
                    escalation_count += 1
                    if escalation_count > MAX_PERMISSION_ESCALATIONS:
                        return await self._record_node_wave_escalation_limit(
                            task_id, selection.get("node_id")
                        )
                    if not await self._handle_permission_escalation(
                        task_id, blocked_node_id=selection.get("node_id")
                    ):
                        return {"status": "rejected", "summary": "Permission escalation rejected."}
                    await workflow.execute_activity(
                        "provision_workspace", task_id, **activity_options("provision_workspace")
                    )
                    continue
                result_ref = None
                if action == "execute":
                    activity_request = selection.get("activity_request")
                    if activity_request is None:
                        failure = "Node selection omitted its activity request."
                        await workflow.execute_activity(
                            "record_workflow_failure",
                            args=[task_id, failure],
                            **activity_options("record_workflow_failure"),
                        )
                        return {"status": "failed", "summary": failure}
                    result_ref = await workflow.execute_activity(
                        "run_decomposed_node",
                        args=[task_id, activity_request],
                        **activity_options(
                            "run_decomposed_node", task_queue=selection.get("execution_task_queue")
                        ),
                    )
                merge = await workflow.execute_activity(
                    "merge_node_wave",
                    args=[task_id, {"selection": selection, "result_ref": result_ref}],
                    **activity_options("merge_node_wave"),
                )
            continuation = merge.get("continuation")
            if continuation in {"continue", "retry_node"}:
                continue
            if continuation == "await_permission":
                self.escalation_count = escalation_count + 1
                escalation_count += 1
                if escalation_count > MAX_PERMISSION_ESCALATIONS:
                    return await self._record_node_wave_escalation_limit(
                        task_id, merge.get("blocked_node_id")
                    )
                if not await self._handle_permission_escalation(
                    task_id, blocked_node_id=merge.get("blocked_node_id")
                ):
                    return {"status": "rejected", "summary": "Permission escalation rejected."}
                await workflow.execute_activity(
                    "provision_workspace", task_id, **activity_options("provision_workspace")
                )
                continue
            failure = "Decomposed node execution failed."
            await workflow.execute_activity(
                "record_workflow_failure",
                args=[task_id, failure],
                **activity_options("record_workflow_failure"),
            )
            return {"status": "failed", "summary": failure}

    async def _run_fanout_wave(self, task_id: str, selection: dict) -> dict:
        """Run every selected activity before a single ordered reconciliation.

        The selector persists the rollout decision, so this method remains fully
        deterministic during replay and never consults process configuration.
        """
        items = selection.get("items") or []
        if not items:
            return {"continuation": "fail_task"}
        tasks = [
            workflow.execute_activity(
                "run_decomposed_node",
                args=[task_id, item["activity_request"]],
                **activity_options("run_decomposed_node", task_queue=item["execution_task_queue"]),
            )
            for item in items
        ]
        # gather preserves selection order even when activities complete out of order.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # A sibling can commit its terminal result before another Activity
        # raises. Preserve those durable references and let the merge Activity
        # project all available evidence before failing the parent task.
        result_refs = [None if isinstance(result, BaseException) else result for result in results]
        return await workflow.execute_activity(
            "merge_node_wave",
            args=[task_id, {"selection": selection, "result_refs": result_refs}],
            **activity_options("merge_node_wave"),
        )

    async def _record_node_wave_escalation_limit(
        self, task_id: str, blocked_node_id: str | None
    ) -> dict:
        """Project a bounded terminal result for repeated node permission requests."""
        failure = (
            "Maximum sequential permission escalation limit reached "
            f"({MAX_PERMISSION_ESCALATIONS})."
        )
        if blocked_node_id is not None:
            await workflow.execute_activity(
                "fail_node_permission_escalation",
                args=[task_id, blocked_node_id],
                **activity_options("fail_node_permission_escalation"),
            )
        await workflow.execute_activity(
            "record_workflow_failure",
            args=[task_id, failure],
            **activity_options("record_workflow_failure"),
        )
        return {"status": "failed", "summary": failure}

    async def _run_worker_with_permission_escalations(
        self, task_id: str, task_queue: str | None
    ) -> dict | None:
        """Run the worker while bounding repeated permission requests."""
        escalation_count = 0
        while True:
            worker_result = await self._run_worker(task_id, task_queue)
            if not worker_result.get("requires_permission_escalation", False):
                return None
            self.escalation_count = escalation_count + 1
            escalation_count += 1
            if escalation_count > MAX_PERMISSION_ESCALATIONS:
                failure = (
                    "Maximum sequential permission escalation limit reached "
                    f"({MAX_PERMISSION_ESCALATIONS})."
                )
                await workflow.execute_activity(
                    "record_workflow_failure",
                    args=[task_id, failure],
                    **activity_options("record_workflow_failure"),
                )
                return {"status": "failed", "summary": failure}
            permission_granted = await self._handle_permission_escalation(task_id)
            if not permission_granted:
                return {"status": "rejected", "summary": "Permission escalation rejected."}
            await workflow.execute_activity(
                "provision_workspace",
                task_id,
                **activity_options("provision_workspace"),
            )

    async def _run_worker(self, task_id: str, task_queue: str | None) -> dict:
        return await workflow.execute_activity(
            "run_worker",
            task_id,
            **activity_options("run_worker", task_queue=task_queue),
        )

    async def _handle_permission_escalation(
        self, task_id: str, blocked_node_id: str | None = None
    ) -> bool:
        self.permission_escalation_decision = None
        prev_stage = self.lifecycle_stage
        self.lifecycle_stage = "awaiting_permission_escalation"
        self.pending_decision = {
            "kind": "permission_escalation",
            "node_id": blocked_node_id,
            "escalation_count": self.escalation_count,
        }
        try:
            await workflow.execute_activity(
                "request_permission_escalation",
                task_id,
                **activity_options("request_permission_escalation"),
            )
            await workflow.wait_condition(lambda: self.permission_escalation_decision is not None)
            approved = self.permission_escalation_decision
            await workflow.execute_activity(
                "resolve_permission_escalation",
                args=[task_id, approved],
                **activity_options("resolve_permission_escalation"),
            )
            return bool(approved)
        finally:
            self.pending_decision = None
            self.lifecycle_stage = prev_stage

    @workflow.query
    def get_status(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "lifecycle_stage": self.lifecycle_stage,
            "escalation_count": self.escalation_count,
            "completion_continuation": self.completion_continuation,
            "has_pending_decision": self.pending_decision is not None,
        }

    @workflow.query
    def get_current_plan(self) -> dict[str, Any]:
        return dict(self.current_plan)

    @workflow.query
    def get_pending_decision(self) -> dict[str, Any] | None:
        return dict(self.pending_decision) if self.pending_decision is not None else None

    @workflow.update
    def apply_operator_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        decision_kind = payload.get("decision_kind") or payload.get("kind")
        command_key = payload.get("command_key")
        if isinstance(command_key, str) and command_key:
            if command_key in self.processed_command_keys:
                return {
                    "applied": False,
                    "duplicate": True,
                    "decision_kind": decision_kind,
                    "lifecycle_stage": self.lifecycle_stage,
                }
            self.processed_command_keys.add(command_key)

        value = payload.get("value", True)
        if decision_kind == "approval":
            self.approval_decision = bool(value)
        elif decision_kind == "clarification":
            self.clarification_resolved = True
        elif decision_kind == "permission_escalation":
            self.permission_escalation_decision = bool(value)

        return {
            "applied": True,
            "duplicate": False,
            "decision_kind": decision_kind,
            "lifecycle_stage": self.lifecycle_stage,
        }

    @apply_operator_decision.validator
    def validate_operator_decision(self, payload: dict[str, Any]) -> None:
        allowed_kinds = {"approval", "clarification", "permission_escalation"}
        decision_kind = payload.get("decision_kind") or payload.get("kind")
        if decision_kind not in allowed_kinds:
            raise ValueError(
                f"Invalid decision_kind '{decision_kind}'. Expected one of {allowed_kinds}."
            )

        command_key = payload.get("command_key")
        if (
            isinstance(command_key, str)
            and command_key
            and command_key in self.processed_command_keys
        ):
            return

        if self.pending_decision is None or self.pending_decision.get("kind") != decision_kind:
            current = self.pending_decision.get("kind") if self.pending_decision else None
            raise ValueError(
                f"Workflow is not awaiting '{decision_kind}'. Current pending decision: {current}."
            )

    @workflow.signal
    async def handle_approval(self, approved: Any) -> None:
        accepted, value = self._accept_signal(approved)
        if accepted:
            self.approval_decision = bool(value)

    @workflow.signal
    async def handle_clarification(self, response: Any = None) -> None:
        accepted, _ = self._accept_signal(response)
        if accepted:
            self.clarification_resolved = True

    @workflow.signal
    async def handle_permission_escalation(self, approved: Any) -> None:
        accepted, value = self._accept_signal(approved)
        if accepted:
            self.permission_escalation_decision = bool(value)

    def _accept_signal(self, payload: Any) -> tuple[bool, Any]:
        """Deduplicate new command envelopes while accepting historical raw signals."""
        if not isinstance(payload, dict) or "command_key" not in payload:
            return True, payload
        command_key = payload.get("command_key")
        if not isinstance(command_key, str) or not command_key:
            return True, payload.get("value")
        if command_key in self.processed_command_keys:
            return False, payload.get("value")
        self.processed_command_keys.add(command_key)
        return True, payload.get("value")
