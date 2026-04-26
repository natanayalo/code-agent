export enum TaskStatus {
  PENDING = 'pending',
  IN_PROGRESS = 'in_progress',
  COMPLETED = 'completed',
  FAILED = 'failed',
  CANCELLED = 'cancelled',
}

export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'not_required';

export interface TaskSummarySnapshot {
  task_id: string;
  session_id: string;
  status: TaskStatus;
  task_text: string;
  repo_url?: string | null;
  branch?: string | null;
  priority: number;
  chosen_worker?: string | null;
  route_reason?: string | null;
  created_at: string;
  updated_at: string;
  latest_run_id?: string | null;
  latest_run_status?: string | null;
  latest_run_worker?: string | null;
  latest_run_requested_permission?: string | null;
  approval_status?: ApprovalStatus | null;
  approval_type?: string | null;
  approval_reason?: string | null;
}
