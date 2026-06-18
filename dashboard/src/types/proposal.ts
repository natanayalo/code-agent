export enum ProposalStatus {
  PENDING_REVIEW = 'pending_review',
  ACCEPTED = 'accepted',
  REJECTED = 'rejected',
  IMPLEMENTED = 'implemented',
}

export enum ProposalType {
  SCOUT = 'scout',
  REFLECTION = 'reflection',
}

export interface ImprovementSuggestionMetadata {
  title?: string;
  description?: string;
  value?: string;
  effort?: string;
  risk?: string;
  layer_impact?: string;
  validation_path?: string;
  hitl_need?: string;
}

export interface FrictionReportMetadata {
  task_id?: string | null;
  worker_run_id?: string | null;
  source?: string | null;
  description?: string | null;
  impact?: string | null;
  context?: Record<string, unknown> | null;
}

export interface ImprovementScoringMetadata {
  enabled?: boolean;
  mode?: string;
  provider?: string | null;
  rationale?: string | null;
  fallback?: boolean;
  fallback_reason?: string | null;
}

export interface ProposalMetadataPayload extends Record<string, unknown> {
  improvement_suggestion?: unknown;
  friction_report?: unknown;
  scoring?: unknown;
  files_changed?: unknown;
  diff_text?: unknown;
}

export interface ProposalSnapshot {
  proposal_id: string;
  session_id: string;
  task_id: string | null;
  title: string;
  summary: string;
  content: string | null;
  status: ProposalStatus | string;
  proposal_type: ProposalType | string;
  metadata_payload: ProposalMetadataPayload;
  created_at: string;
  updated_at: string;
}
