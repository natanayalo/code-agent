export interface OperationalMetrics {
  total_tasks: number;
  retried_tasks: number;
  retry_rate: number;
  status_counts: Record<string, number>;
  worker_usage: Record<string, number>;
  avg_duration_seconds: number;
  success_rate: number;
}
