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

export type ImprovementMetadataScalar = string | number | boolean;

export interface ImprovementSuggestionMetadata {
  title?: string;
  description?: string;
  value?: ImprovementMetadataScalar;
  effort?: ImprovementMetadataScalar;
  risk?: ImprovementMetadataScalar;
  layer_impact?: ImprovementMetadataScalar;
  validation_path?: ImprovementMetadataScalar;
  hitl_need?: ImprovementMetadataScalar;
}

export interface ScoutProposalMetadata {
  title?: string;
  description?: string;
  value?: ImprovementMetadataScalar;
  effort?: ImprovementMetadataScalar;
  risk?: ImprovementMetadataScalar;
  layer_impact?: ImprovementMetadataScalar;
  validation_path?: ImprovementMetadataScalar;
  hitl_need?: ImprovementMetadataScalar;
  evidence?: string[];
  implementation_slice?: string;
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
  scout_proposal?: unknown;
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
