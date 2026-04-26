export enum SessionStatus {
  ACTIVE = 'active',
  CLOSED = 'closed',
}

export interface SessionSnapshot {
  session_id: string;
  user_id: string;
  channel: string;
  external_thread_id: string;
  active_task_id?: string | null;
  status: SessionStatus;
  last_seen_at?: string | null;
  created_at: string;
  updated_at: string;
}
