"""Provider readiness double for integration tests using stub workers."""

from db.base import utc_now
from orchestrator.provider_diagnostics import ProviderDiagnosticsService
from orchestrator.provider_diagnostics_types import (
    ProviderExecutionContext,
    ProviderPreDispatchReport,
    compute_report_id,
)


async def ready_stub_provider(
    _service: ProviderDiagnosticsService, context: ProviderExecutionContext
) -> ProviderPreDispatchReport:
    """Return a successful readiness report without probing local providers."""
    return ProviderPreDispatchReport(
        report_id=compute_report_id(context.logical_execution_key or "unknown"),
        checked_at=utc_now(),
        execution_environment_id="test",
        logical_execution_key=context.logical_execution_key or "test",
        provider=context.provider,
        worker_profile=context.worker_profile,
        runtime_mode=context.runtime_mode,
        model=context.model,
        decision="preflight_passed",
        ready=True,
        execution_started=False,
    )
