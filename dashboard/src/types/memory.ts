export interface MemoryMetadata {
  source?: string | null;
  confidence: number;
  scope?: string | null;
  last_verified_at?: string | null;
  requires_verification: boolean;
}

export interface PersonalMemorySnapshot extends MemoryMetadata {
  memory_id: string;
  memory_key: string;
  value: Record<string, unknown>;
  headline?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectMemorySnapshot extends MemoryMetadata {
  memory_id: string;
  repo_url: string;
  memory_key: string;
  value: Record<string, unknown>;
  headline?: string | null;
  created_at: string;
  updated_at: string;
}

export interface MemoryInventoryCountSnapshot {
  total: number;
  requires_verification: number;
}

export interface KnowledgeBaseStatsSnapshot {
  personal: MemoryInventoryCountSnapshot;
  project: MemoryInventoryCountSnapshot | null;
  project_global: MemoryInventoryCountSnapshot;
}

export type MemoryProposalCategory = 'personal' | 'project';
export type MemoryProposalStatus = 'pending_review' | 'accepted' | 'rejected';

export interface MemoryProposalSnapshot extends MemoryMetadata {
  proposal_id: string;
  category: MemoryProposalCategory;
  repo_url?: string | null;
  memory_key: string;
  value: Record<string, unknown>;
  status: MemoryProposalStatus;
  title?: string | null;
  summary?: string | null;
  evidence?: Record<string, unknown> | null;
  task_id?: string | null;
  session_id?: string | null;
  source_observation_id?: string | null;
  accepted_memory_id?: string | null;
  reviewed_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface MemoryObservationSnapshot {
  observation_id: string;
  task_id?: string | null;
  session_id?: string | null;
  repo_url?: string | null;
  worker_type?: string | null;
  source: string;
  event_type: string;
  observed_at: string;
  summary: string;
  content: string;
  metadata_payload: Record<string, unknown>;
  privacy_stripped: boolean;
  admission_status: string;
  admission_processed_at?: string | null;
  admission_error?: string | null;
  decision_id?: string | null;
  proposal_id?: string | null;
  durable_memory_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface MemoryAdmissionDecisionSnapshot {
  decision_id: string;
  category: string;
  memory_key: string;
  candidate_payload: Record<string, unknown>;
  decision: string;
  risk_level: string;
  reason: string;
  task_id?: string | null;
  session_id?: string | null;
  repo_url?: string | null;
  durable_memory_id?: string | null;
  proposal_id?: string | null;
  source_observation_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface MemoryProposalCreateRequest {
  category: MemoryProposalCategory;
  repo_url?: string | null;
  memory_key: string;
  value: Record<string, unknown>;
  source?: string | null;
  confidence?: number;
  scope?: string | null;
  requires_verification?: boolean;
  title?: string | null;
  summary?: string | null;
  evidence?: Record<string, unknown> | null;
  task_id?: string | null;
  session_id?: string | null;
}

export interface PersonalMemoryUpsertRequest {
  memory_key: string;
  value: Record<string, unknown>;
  source?: string | null;
  confidence?: number;
  scope?: string | null;
  last_verified_at?: string | null;
  requires_verification?: boolean;
}

export interface ProjectMemoryUpsertRequest {
  repo_url: string;
  memory_key: string;
  value: Record<string, unknown>;
  source?: string | null;
  confidence?: number;
  scope?: string | null;
  last_verified_at?: string | null;
  requires_verification?: boolean;
}
