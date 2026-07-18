from __future__ import annotations

import asyncio

from temporalio import workflow

from orchestrator.temporal.policy import activity_options

MAX_PERMISSION_ESCALATIONS = 5
NODE_WAVE_PATCH_ID = "m25-1b-temporal-node-wave"
M25_2_FANOUT_PATCH_ID = "m25-2-bounded-selective-fanout"


@workflow.defn
class TaskExecutionWorkflow:
    def __init__(self) -> None:
        self.approval_decision: bool | None = None
        self.clarification_resolved = False
        self.permission_escalation_decision: bool | None = None

    @workflow.run
    async def run(self, task_id: str) -> dict:
        try:
            return await self._run_lifecycle(task_id)
        except Exception as exc:
            await workflow.execute_activity(
                "record_workflow_failure",
                args=[task_id, str(exc)],
                **activity_options("record_workflow_failure"),
            )
            return {"status": "failed", "summary": "Temporal workflow activity failed."}

    async def _run_lifecycle(self, task_id: str) -> dict:
        # Step 1: Classify and Plan
        res = await workflow.execute_activity(
            "classify_and_plan",
            task_id,
            **activity_options("classify_and_plan"),
        )
        requires_clarification = res.get("requires_clarification", False)
        requires_approval = res.get("requires_approval", False)
        execution_task_queue = res.get("execution_task_queue")

        # Step 2: clarification check
        if requires_clarification:
            await workflow.wait_condition(lambda: self.clarification_resolved)

        # Step 3: approval check
        if requires_approval:
            # Wait for approval signal
            await workflow.wait_condition(lambda: self.approval_decision is not None)
            if not self.approval_decision:
                return {"status": "rejected", "summary": "Manual approval rejected."}

        # Step 4: Decompose Task
        decomposition = await workflow.execute_activity(
            "decompose_task",
            task_id,
            **activity_options("decompose_task"),
        )

        # Step 5: Load memory
        await workflow.execute_activity(
            "load_memory",
            task_id,
            **activity_options("load_memory"),
        )

        # Step 6: Provision workspace
        await workflow.execute_activity(
            "provision_workspace",
            task_id,
            **activity_options("provision_workspace"),
        )

        # Step 7: Existing histories preserve their recorded worker activity.
        # New histories enter the one-node-wave coordinator only for a validated DAG.
        use_node_waves = workflow.patched(NODE_WAVE_PATCH_ID) and (
            (decomposition or {}).get("execution_shape") == "decomposed"
        )
        escalation_failure = (
            await self._run_decomposed_node_waves(task_id)
            if use_node_waves
            else await self._run_worker_with_permission_escalations(task_id, execution_task_queue)
        )
        if escalation_failure is not None:
            return escalation_failure

        # Step 8: Verify result
        await workflow.execute_activity(
            "verify_result",
            task_id,
            **activity_options("verify_result"),
        )

        # Step 9: Persist memory before terminal delivery so the final worker
        # result remains available in the Temporal snapshot.
        await workflow.execute_activity(
            "persist_memory",
            task_id,
            **activity_options("persist_memory"),
        )

        # Step 10: Deliver result and remove the completed snapshot.
        await workflow.execute_activity(
            "deliver_result",
            task_id,
            **activity_options("deliver_result"),
        )

        return {"status": "completed", "summary": "Task completed successfully via Temporal."}

    async def _run_decomposed_node_waves(self, task_id: str) -> dict | None:
        """Coordinate exactly one durable node execution before every merge."""
        escalation_count = 0
        # Record the M25.2 marker before selection can produce a V2 contract.
        # Passing this recorded decision to the Activity keeps a replay after a
        # workflow-task crash on the same contract version as the original run.
        fanout_contract_enabled = workflow.patched(M25_2_FANOUT_PATCH_ID)
        while True:
            # The legacy selector's input is part of M25.1B history. Keep its
            # one-argument command byte-for-byte compatible for patch-false
            # replays; V2 uses a separately named Activity contract.
            if fanout_contract_enabled:
                selection = await workflow.execute_activity(
                    "select_next_node_v2",
                    task_id,
                    **activity_options("select_next_node_v2"),
                )
            else:
                selection = await workflow.execute_activity(
                    "select_next_node",
                    task_id,
                    **activity_options("select_next_node"),
                )
            if (
                fanout_contract_enabled
                and selection.get("schema_version") == 2
                and selection.get("fanout_applied")
            ):
                merge = await self._run_fanout_wave(task_id, selection)
            else:
                action = selection.get("action")
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
                    escalation_count += 1
                    if escalation_count > MAX_PERMISSION_ESCALATIONS:
                        return await self._record_node_wave_escalation_limit(
                            task_id, selection.get("node_id")
                        )
                    if not await self._handle_permission_escalation(task_id):
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
                escalation_count += 1
                if escalation_count > MAX_PERMISSION_ESCALATIONS:
                    return await self._record_node_wave_escalation_limit(
                        task_id, merge.get("blocked_node_id")
                    )
                if not await self._handle_permission_escalation(task_id):
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

    async def _handle_permission_escalation(self, task_id: str) -> bool:
        self.permission_escalation_decision = None
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

    @workflow.signal
    async def handle_approval(self, approved: bool) -> None:
        self.approval_decision = approved

    @workflow.signal
    async def handle_clarification(self, _response: object | None = None) -> None:
        self.clarification_resolved = True

    @workflow.signal
    async def handle_permission_escalation(self, approved: bool) -> None:
        self.permission_escalation_decision = approved
