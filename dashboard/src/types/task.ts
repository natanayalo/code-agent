export enum TaskStatus {
  PENDING = 'pending',
  IN_PROGRESS = 'in_progress',
  COMPLETED = 'completed',
  FAILED = 'failed',
  CANCELLED = 'cancelled',
}

export const WORKER_OPTIONS = ['codex', 'antigravity', 'openrouter'] as const;
export type WorkerType = (typeof WORKER_OPTIONS)[number];

export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'not_required';

export type TaskRiskLevel = 'low' | 'medium' | 'high' | 'critical';
export type TaskSpecType =
  | 'docs'
  | 'bugfix'
  | 'feature'
  | 'refactor'
  | 'investigation'
  | 'review_fix'
  | 'maintenance'
  | 'scout';
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
  repair_for_task_id?: string | null;
  chosen_worker?: string | null;
  chosen_profile?: string | null;
  runtime_mode?: string | null;
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
  trace_id?: string | null;
  trace_url?: string | null;
}

export interface DeliveryMetadataSnapshot {
  branch_name?: string | null;
  pr_title?: string | null;
  pr_url?: string | null;
  pr_number?: number | null;
  head_sha?: string | null;
  ci_status?: string | null;
  ci_failed_jobs?: string[] | null;
  [key: string]: unknown;
}

export interface WorkerRunSnapshot {
  run_id: string;
  session_id?: string | null;
  worker_type: string;
  worker_profile?: string | null;
  runtime_mode?: string | null;
  workspace_id?: string | null;
  status: string;
  started_at: string;
  finished_at?: string | null;
  summary?: string | null;
  requested_permission?: string | null;
  budget_usage?: Record<string, unknown> | null;
  verifier_outcome?: Record<string, unknown> | null;
  delivery_metadata?: DeliveryMetadataSnapshot | null;
  commands_run: CommandRunSnapshot[];
  files_changed_count: number;
  files_changed: string[];
  artifact_index: ArtifactIndexEntry[];
  artifacts: ArtifactSnapshot[];
}

export interface CommandRunSnapshot {
  id: string;
  command?: string;
  exit_code?: number;
  duration_seconds?: number;
  stdout_artifact_uri?: string;
  stderr_artifact_uri?: string;
  timed_out?: boolean;
}

export interface ArtifactIndexEntry {
  id: string;
  name?: string;
  uri?: string;
  artifact_type?: string;
  artifact_metadata?: Record<string, unknown> | null;
}

export interface ArtifactSnapshot {
  artifact_id: string;
  artifact_type: string;
  name: string;
  uri: string;
  artifact_metadata?: Record<string, unknown> | null;
}

export interface TaskTimelineEventSnapshot {
  id: string;
  event_type: string;
  attempt_number?: number;
  sequence_number?: number;
  message?: string | null;
  payload?: Record<string, unknown> | null;
  created_at: string;
}

export interface VerifierOutcomeItem {
  id: string;
  label: string;
  status: string;
  message: string | null;
}

export interface VerifierOutcomeSnapshot {
  status: string | null;
  summary: string | null;
  items: VerifierOutcomeItem[];
}

export interface HumanInteractionSnapshot {
  interaction_id: string;
  interaction_type: string;
  status: string;
  summary: string;
  decision_key?: string | null;
  hitl_mode: string;
  data: Record<string, unknown>;
  response_data?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface InteractionInboxCard {
  interaction: HumanInteractionSnapshot;
  task_id: string;
  task_text: string;
  status: string;
  repo_url?: string | null;
  branch?: string | null;
  priority: number;
}

export enum ExecutionPlanNodeStatus {
  PENDING = 'pending',
  ACTIVE = 'active',
  BLOCKED = 'blocked',
  COMPLETED = 'completed',
  FAILED = 'failed',
  SKIPPED = 'skipped'
}

export interface ExecutionPlanNodeSnapshot {
  node_id: string;
  goal: string;
  status:
    | ExecutionPlanNodeStatus
    | 'pending'
    | 'active'
    | 'blocked'
    | 'completed'
    | 'failed'
    | 'skipped';
  acceptance_criteria?: string | null;
  depends_on?: string[] | null;
  task_spec?: TaskSpec | null;
  node_kind?: string | null;
  aggregation_role?: 'context' | 'mutation' | 'validation' | 'final';
  execution_mode?: 'read_only' | 'mutable';
  parallel_safe?: boolean;
  assigned_worker_profile?: string | null;
  budget?: Record<string, unknown> | null;
  validation_commands?: string[] | null;
  artifacts?: string[] | null;
  blocker_interaction_id?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  worker_run_id?: string | null;
  result_summary?: string | null;
  failure_kind?: string | null;
  verification_outcome?: Record<string, unknown> | null;
  changed_files?: string[] | null;
  output_artifacts?: Record<string, unknown>[] | null;
  last_attempt_at?: string | null;
  attempts?: ExecutionPlanNodeAttemptSnapshot[];
  retry_count: number;
  created_at: string;
  updated_at: string;
}

export interface ExecutionPlanNodeAttemptSnapshot {
  attempt_number: number;
  started_at: string;
  finished_at?: string | null;
  duration_ms?: number | null;
  worker_run_id?: string | null;
  task_trace_id?: string | null;
  worker_type?: string | null;
  worker_profile?: string | null;
  runtime_mode?: string | null;
  workspace_id?: string | null;
  status: string;
  failure_kind?: string | null;
  effective_input_summary: Record<string, unknown>;
  effective_input_digest: string;
}

export interface ExecutionPlanSnapshot {
  plan_id: string;
  task_id: string;
  created_at: string;
  updated_at: string;
  nodes: ExecutionPlanNodeSnapshot[];
}

export interface TaskSnapshot extends TaskSummarySnapshot {
  task_spec?: TaskSpec | null;
  latest_run?: WorkerRunSnapshot | null;
  pending_interactions?: HumanInteractionSnapshot[];
  timeline: TaskTimelineEventSnapshot[];
  execution_plan?: ExecutionPlanSnapshot | null;
}

export interface TaskReplayRequest {
  worker_override?: WorkerType;
  constraints?: Record<string, unknown>;
  budget?: Record<string, unknown>;
  secrets?: Record<string, string>;
}

export interface TaskSubmissionSessionRequest {
  channel?: string;
  external_user_id?: string;
  external_thread_id?: string;
  display_name?: string | null;
}

export interface TaskSubmissionRequest {
  task_text: string;
  repo_url?: string | null;
  branch?: string | null;
  priority?: number;
  worker_override?: WorkerType;
  worker_profile_override?: string;
  constraints?: Record<string, unknown>;
  budget?: Record<string, unknown>;
  tools?: string[] | null;
  callback_url?: string | null;
  session?: TaskSubmissionSessionRequest;
}

export interface ScoutTriggerRequest {
  mode?: 'repo' | 'research' | 'deep';
  repo_key?: string | null;
  branch?: string | null;
  focus?: string | null;
  depth?: 'shallow' | 'standard' | 'deep';
  max_proposals?: number;
}
