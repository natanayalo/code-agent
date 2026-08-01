export type ReadinessStatus = 'ready' | 'not_ready' | 'unknown';

export interface ReadinessComponent {
  status: ReadinessStatus;
  reasons: string[];
  last_observed_at: string | null;
}

export interface ReadinessSnapshot {
  status: 'ready' | 'not_ready';
  checked_at: string;
  components: Record<string, ReadinessComponent>;
  degraded_reasons: string[];
}

export interface OutboxHealthMetrics {
  pending_count: number;
  retrying_count: number;
  dead_letter_count: number;
  oldest_unresolved_age_seconds: number | null;
  oldest_eligible_age_seconds: number | null;
  affected_task_ids: string[];
  affected_task_ids_truncated: boolean;
}

export interface WorkerHealthMetrics {
  fresh_count: number;
  stale_count: number;
  fresh_dispatcher_count: number;
  freshest_heartbeat_at: string | null;
  freshest_heartbeat_age_seconds: number | null;
  freshest_dispatcher_heartbeat_at: string | null;
  freshest_dispatcher_heartbeat_age_seconds: number | null;
}

export interface InteractionWaitMetrics {
  pending_count: number;
  stuck_count: number;
  oldest_pending_age_seconds: number | null;
  affected_task_ids: string[];
  affected_task_ids_truncated: boolean;
}

export interface TerminalReconciliationMetrics {
  status: 'ok' | 'degraded' | 'unknown';
  divergence_count: number | null;
  affected_task_ids: string[];
  affected_task_ids_truncated: boolean;
  checked_at: string;
}

export interface ExecutionHealthMetrics {
  outbox: OutboxHealthMetrics;
  workers: WorkerHealthMetrics;
  interactions: InteractionWaitMetrics;
  reconciliation: TerminalReconciliationMetrics;
  degraded_reasons: string[];
}

export interface OperationalMetrics {
  total_tasks: number;
  retried_tasks: number;
  retry_rate: number;
  status_counts: Record<string, number>;
  worker_usage: Record<string, number>;
  runtime_mode_usage: Record<string, number>;
  legacy_tool_loop_usage: Record<string, number>;
  // Optional during rolling upgrades, when the dashboard can be newer than the API.
  orchestration_runtime_counts?: Record<string, number>;
  active_legacy_task_count?: number;
  active_unknown_task_count?: number;
  temporal_only_cutover_at?: string | null;
  legacy_submissions_since_cutover?: number | null;
  avg_duration_seconds: number;
  success_rate: number;
  // Optional during rolling upgrades, when the dashboard can be newer than the API.
  execution_health?: ExecutionHealthMetrics;
}
