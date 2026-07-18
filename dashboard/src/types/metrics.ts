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
  avg_duration_seconds: number;
  success_rate: number;
}
