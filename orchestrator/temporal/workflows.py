from __future__ import annotations

from temporalio import workflow

from orchestrator.temporal.policy import activity_options


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
        await workflow.execute_activity(
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

        # Step 7: Run worker
        while True:
            worker_result = await self._run_worker(task_id, execution_task_queue)
            if not worker_result.get("requires_permission_escalation", False):
                break
            permission_granted = await self._handle_permission_escalation(task_id)
            if not permission_granted:
                return {"status": "rejected", "summary": "Permission escalation rejected."}
            await workflow.execute_activity(
                "provision_workspace",
                task_id,
                **activity_options("provision_workspace"),
            )

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

    async def _run_worker(self, task_id: str, task_queue: str | None) -> dict:
        return await workflow.execute_activity(
            "run_worker",
            task_id,
            **activity_options("run_worker", task_queue=task_queue),
        )

    async def _handle_permission_escalation(self, task_id: str) -> bool:
        await workflow.execute_activity(
            "request_permission_escalation",
            task_id,
            **activity_options("request_permission_escalation"),
        )
        self.permission_escalation_decision = None
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
