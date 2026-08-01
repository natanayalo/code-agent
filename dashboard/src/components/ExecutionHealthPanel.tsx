import React from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Database,
  HeartPulse,
  Inbox,
  RefreshCw,
  Server,
  Workflow,
} from 'lucide-react';
import {
  ExecutionHealthMetrics,
  ReadinessComponent,
  ReadinessSnapshot,
} from '../types/metrics';
import { formatLabel } from '../utils/formatters';

const DEPENDENCY_ORDER = ['postgres', 'temporal', 'worker', 'dispatcher'];

const RECOVERY_GUIDANCE: Record<string, string> = {
  task_service_unconfigured:
    'Verify the API task-service configuration, then restart the API after correcting its startup settings.',
  postgres_unavailable:
    'Restore Postgres connectivity, then wait for readiness to recover without restarting the API.',
  temporal_unavailable:
    'Restore Temporal and verify cluster health. New submissions remain disabled until a probe succeeds.',
  worker_unavailable:
    'Inspect and restart the worker process. Do not run coding work directly on the host.',
  dispatcher_unavailable:
    'Inspect and restart the worker process that owns command dispatch. Do not deliver outbox commands manually.',
  dispatcher_backlog_stale:
    'Inspect worker logs and outbox errors, then restart the worker if dispatch is not progressing. Do not delete outbox rows.',
  command_retries_present:
    'Inspect the retrying commands and worker logs. Allow bounded retries to continue unless progress has stopped.',
  command_dead_letters_present:
    'Correct the non-retryable cause, then use supported replay controls. Do not edit command rows directly.',
  interaction_wait_stuck:
    'Answer, reject, or cancel the affected interaction through the Tasks view or API.',
  terminal_state_divergence:
    'Compare the task timeline with Temporal state, restore the worker if needed, and avoid direct terminal-state edits.',
  terminal_reconciliation_unknown:
    'Restore Temporal visibility before treating the reconciliation count as healthy.',
};

const UNKNOWN_REASON_GUIDANCE =
  'Inspect readiness, metrics, and the affected task timeline before acting. Do not edit database state directly.';

interface ExecutionHealthPanelProps {
  readiness?: ReadinessSnapshot;
  executionHealth?: ExecutionHealthMetrics;
  readinessLoading: boolean;
  executionHealthLoading: boolean;
  readinessError: unknown;
  executionHealthError: unknown;
  refreshing: boolean;
  onRefresh: () => void;
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return 'Not observed';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Unknown' : parsed.toLocaleString();
}

function formatAge(seconds: number | null): string {
  if (seconds === null) return 'Not observed';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Request failed';
}

function statusClass(status: string): string {
  if (status === 'ready' || status === 'ok') return 'healthy';
  if (status === 'unknown') return 'unknown';
  return 'degraded';
}

function StatusIcon({ status }: { status: string }) {
  return statusClass(status) === 'healthy' ? (
    <CheckCircle2 size={18} aria-hidden="true" />
  ) : (
    <AlertTriangle size={18} aria-hidden="true" />
  );
}

function DependencyCard({ name, component }: { name: string; component?: ReadinessComponent }) {
  const status = component?.status ?? 'unknown';
  return (
    <article className={`health-dependency-card health-state-${statusClass(status)}`}>
      <div className="health-card-heading">
        <StatusIcon status={status} />
        <h4>{formatLabel(name)}</h4>
      </div>
      <strong className="health-state-label">{formatLabel(status)}</strong>
      <p>Last observed: {formatTimestamp(component?.last_observed_at)}</p>
      {component?.reasons.length ? (
        <p className="health-card-reasons">{component.reasons.map(formatLabel).join(', ')}</p>
      ) : null}
    </article>
  );
}

function AffectedTasks({ taskIds, truncated }: { taskIds: string[]; truncated: boolean }) {
  if (!taskIds.length) return null;
  return (
    <div className="health-affected-tasks">
      <span>Affected tasks</span>
      <ul>
        {taskIds.map((taskId) => <li key={taskId}>{taskId}</li>)}
      </ul>
      {truncated ? <small>Additional affected tasks are omitted.</small> : null}
    </div>
  );
}

function HealthMetric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="health-metric-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function HealthSignals({ health }: { health: ExecutionHealthMetrics }) {
  return (
    <div className="health-signals-grid">
      <article className="health-signal-card">
        <div className="health-card-heading"><Workflow size={18} aria-hidden="true" /><h4>Command Outbox</h4></div>
        <dl>
          <HealthMetric label="Pending" value={health.outbox.pending_count} />
          <HealthMetric label="Retrying" value={health.outbox.retrying_count} />
          <HealthMetric label="Dead letters" value={health.outbox.dead_letter_count} />
          <HealthMetric label="Oldest unresolved" value={formatAge(health.outbox.oldest_unresolved_age_seconds)} />
          <HealthMetric label="Oldest eligible" value={formatAge(health.outbox.oldest_eligible_age_seconds)} />
        </dl>
        <AffectedTasks taskIds={health.outbox.affected_task_ids} truncated={health.outbox.affected_task_ids_truncated} />
      </article>

      <article className="health-signal-card">
        <div className="health-card-heading"><HeartPulse size={18} aria-hidden="true" /><h4>Workers & Dispatcher</h4></div>
        <dl>
          <HealthMetric label="Fresh workers" value={health.workers.fresh_count} />
          <HealthMetric label="Stale workers" value={health.workers.stale_count} />
          <HealthMetric label="Fresh dispatchers" value={health.workers.fresh_dispatcher_count} />
          <HealthMetric label="Worker heartbeat age" value={formatAge(health.workers.freshest_heartbeat_age_seconds)} />
          <HealthMetric label="Dispatcher heartbeat age" value={formatAge(health.workers.freshest_dispatcher_heartbeat_age_seconds)} />
        </dl>
      </article>

      <article className="health-signal-card">
        <div className="health-card-heading"><Inbox size={18} aria-hidden="true" /><h4>Interaction Waits</h4></div>
        <dl>
          <HealthMetric label="Pending" value={health.interactions.pending_count} />
          <HealthMetric label="Stuck" value={health.interactions.stuck_count} />
          <HealthMetric label="Oldest pending" value={formatAge(health.interactions.oldest_pending_age_seconds)} />
        </dl>
        <AffectedTasks taskIds={health.interactions.affected_task_ids} truncated={health.interactions.affected_task_ids_truncated} />
      </article>

      <article className={`health-signal-card health-state-${statusClass(health.reconciliation.status)}`}>
        <div className="health-card-heading"><Activity size={18} aria-hidden="true" /><h4>Terminal Reconciliation</h4></div>
        <dl>
          <HealthMetric label="Status" value={formatLabel(health.reconciliation.status)} />
          <HealthMetric label="Divergences" value={health.reconciliation.divergence_count ?? 'Unknown'} />
          <HealthMetric label="Checked" value={formatTimestamp(health.reconciliation.checked_at)} />
        </dl>
        <AffectedTasks taskIds={health.reconciliation.affected_task_ids} truncated={health.reconciliation.affected_task_ids_truncated} />
      </article>
    </div>
  );
}

function collectReasons(
  readiness?: ReadinessSnapshot,
  executionHealth?: ExecutionHealthMetrics,
): string[] {
  const componentReasons = Object.values(readiness?.components ?? {}).flatMap(
    (component) => component.reasons,
  );
  return Array.from(new Set([
    ...(readiness?.degraded_reasons ?? []),
    ...componentReasons,
    ...(executionHealth?.degraded_reasons ?? []),
  ]));
}

export function ExecutionHealthPanel({
  readiness,
  executionHealth,
  readinessLoading,
  executionHealthLoading,
  readinessError,
  executionHealthError,
  refreshing,
  onRefresh,
}: ExecutionHealthPanelProps) {
  const reasons = collectReasons(readiness, executionHealth);
  const overallStatus = readiness?.status ?? 'unknown';
  const overallLabel = readinessLoading && !readiness
    ? 'Checking execution readiness'
    : overallStatus === 'ready'
      ? 'Execution ready'
      : overallStatus === 'not_ready'
        ? 'Execution blocked'
        : 'Readiness unknown';

  return (
    <section className="execution-health-panel" aria-labelledby="execution-health-heading">
      <div className="execution-health-header">
        <div>
          <div className="health-title-row">
            <Server size={22} aria-hidden="true" />
            <h2 id="execution-health-heading">Execution Status</h2>
          </div>
          <p>Live dependency state, stuck-work signals, and safe recovery guidance.</p>
        </div>
        <button
          type="button"
          className="health-refresh-button"
          onClick={onRefresh}
          disabled={refreshing}
          aria-label="Refresh execution status"
        >
          <RefreshCw size={16} className={refreshing ? 'spin' : undefined} aria-hidden="true" />
          {refreshing ? 'Refreshing...' : 'Refresh status'}
        </button>
      </div>

      <div
        className={`health-overview health-state-${statusClass(overallStatus)}`}
        role={overallStatus === 'not_ready' ? 'alert' : 'status'}
        aria-live="polite"
      >
        <StatusIcon status={overallStatus} />
        <div>
          <strong>{overallLabel}</strong>
          <span><Clock3 size={14} aria-hidden="true" /> Checked {formatTimestamp(readiness?.checked_at)}</span>
        </div>
      </div>

      {readinessError && !readiness ? (
        <div className="health-fetch-error" role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          Readiness probe unavailable: {errorMessage(readinessError)}
        </div>
      ) : null}

      {readiness ? (
        <div className="health-dependencies-grid" aria-label="Execution dependencies">
          {DEPENDENCY_ORDER.map((name) => (
            <DependencyCard key={name} name={name} component={readiness.components?.[name]} />
          ))}
        </div>
      ) : null}

      <div className="health-section-heading">
        <Database size={18} aria-hidden="true" />
        <h3>Operational Signals</h3>
      </div>
      {executionHealthLoading && !executionHealth ? (
        <div className="health-loading-state" role="status">Loading operational signals...</div>
      ) : executionHealth ? (
        <HealthSignals health={executionHealth} />
      ) : (
        <div className="health-fetch-error" role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          Operational signals unavailable{executionHealthError ? `: ${errorMessage(executionHealthError)}` : '.'}
        </div>
      )}

      <div className="health-section-heading">
        <AlertTriangle size={18} aria-hidden="true" />
        <h3>Recovery Guidance</h3>
      </div>
      {reasons.length ? (
        <div className="recovery-guidance-list">
          {reasons.map((reason) => (
            <article key={reason} className="recovery-guidance-item">
              <strong>{formatLabel(reason)}</strong>
              <p>{RECOVERY_GUIDANCE[reason] ?? UNKNOWN_REASON_GUIDANCE}</p>
              <code>{reason}</code>
            </article>
          ))}
        </div>
      ) : readiness && executionHealth ? (
        <div className="health-clear-state">
          <CheckCircle2 size={18} aria-hidden="true" /> No degraded conditions reported.
        </div>
      ) : (
        <div className="health-clear-state health-state-unknown">
          Recovery guidance will appear when status data is available.
        </div>
      )}
    </section>
  );
}
