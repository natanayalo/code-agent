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
  accepted_memory_id?: string | null;
  reviewed_at?: string | null;
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
