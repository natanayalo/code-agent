export enum ProposalStatus {
  PENDING_REVIEW = 'pending_review',
  ACCEPTED = 'accepted',
  REJECTED = 'rejected',
  IMPLEMENTED = 'implemented',
}

export interface ProposalSnapshot {
  proposal_id: string;
  session_id: string;
  task_id: string | null;
  title: string;
  summary: string;
  content: string | null;
  status: ProposalStatus | string;
  metadata_payload: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}
