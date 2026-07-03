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
