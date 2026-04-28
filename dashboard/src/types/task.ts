export enum TaskStatus {
  PENDING = 'pending',
  IN_PROGRESS = 'in_progress',
  COMPLETED = 'completed',
  FAILED = 'failed',
  CANCELLED = 'cancelled',
}

export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'not_required';

export type TaskRiskLevel = 'low' | 'medium' | 'high' | 'critical';
export type TaskSpecType =
  | 'docs'
  | 'bugfix'
  | 'feature'
  | 'refactor'
  | 'investigation'
  | 'review_fix'
  | 'maintenance';
export type TaskDeliveryMode = 'summary' | 'workspace' | 'branch' | 'draft_pr';

export interface TaskSpec {
  goal: string;
  repo_url?: string | null;
  target_branch?: string | null;
  assumptions: string[];
  acceptance_criteria: string[];
  non_goals: string[];
  risk_level: TaskRiskLevel;
  task_type: TaskSpecType;
  allowed_actions: string[];
  forbidden_actions: string[];
  verification_commands: string[];
  expected_artifacts: string[];
  requires_clarification: boolean;
  clarification_questions: string[];
  requires_permission: boolean;
  permission_reason?: string | null;
  delivery_mode: TaskDeliveryMode;
}

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
  pending_interaction_count?: number;
  approval_status?: ApprovalStatus | null;
  approval_type?: string | null;
  approval_reason?: string | null;
}
export interface WorkerRunSnapshot {
  run_id: string;
  worker_type: string;
  status: string;
  started_at: string;
  finished_at?: string | null;
  summary?: string | null;
  commands_run: unknown[];
  files_changed_count: number;
}

export interface TaskTimelineEventSnapshot {
  event_type: string;
  message?: string | null;
  created_at: string;
}

export interface HumanInteractionSnapshot {
  interaction_id: string;
  interaction_type: string;
  status: string;
  summary: string;
  data: Record<string, unknown>;
  response_data?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface TaskSnapshot extends TaskSummarySnapshot {
  task_spec?: TaskSpec | null;
  latest_run?: WorkerRunSnapshot | null;
  pending_interactions?: HumanInteractionSnapshot[];
  timeline: TaskTimelineEventSnapshot[];
}
