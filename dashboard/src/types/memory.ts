export interface MemoryMetadata {
  source?: string | null;
  confidence: number;
  scope?: string | null;
  last_verified_at?: string | null;
  requires_verification: boolean;
}

export interface PersonalMemorySnapshot extends MemoryMetadata {
  memory_id: string;
  user_id: string;
  memory_key: string;
  value: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ProjectMemorySnapshot extends MemoryMetadata {
  memory_id: string;
  repo_url: string;
  memory_key: string;
  value: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface PersonalMemoryUpsertRequest {
  user_id: string;
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
