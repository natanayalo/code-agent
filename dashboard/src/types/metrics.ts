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

export interface ReadinessAlertStateSnapshot {
  reason: string;
  acknowledgment_reason?: string;
  task_id?: string | null;
  worker_node_id?: string | null;
  provider?: string | null;
  capability?: string | null;
  next_action?: string | null;
  degraded: boolean;
  transition_number: number;
  last_transition_at: string;
  last_checked_at: string;
  acknowledged_at?: string | null;
  acknowledged_by?: string | null;
}

export interface ReadinessAlertAcknowledgmentSnapshot {
  reason: string;
  transition_number: number;
  acknowledged_at: string;
  acknowledged_by: string;
}

export interface ReadinessAlertDeliverySnapshot {
  id: string;
  reason: string;
  task_id?: string | null;
  worker_node_id?: string | null;
  provider?: string | null;
  capability?: string | null;
  next_action?: string | null;
  transition_number: number;
  transition: string;
  status: string;
  attempts: number;
  next_attempt_at: string;
  delivered_at: string | null;
  last_failure_code: string | null;
  last_http_status: number | null;
  created_at: string;
}

export interface DependencyReviewScheduleSnapshot {
  id: string;
  spec: {
    version: number;
    purpose: string;
    repo_key: string;
    branch: string | null;
    weekdays: string[];
    time: string;
    timezone: string;
  };
  paused: boolean;
  next_executions: string[];
  recent_actions: Array<{
    scheduled_at: string;
    started_at: string | null;
    occurrence_id: string;
    outcome?: string;
    task_id?: string;
    skipped?: boolean;
    reason?: string;
  }>;
  skipped_overlap: number;
  missed_catchup: number;
}

export interface ScheduledTaskSnapshot {
  task: import('./task').TaskSummarySnapshot;
  status_url: string;
  timeline_url: string;
  artifacts_url: string;
}
