export type MilestoneAutonomyMode =
  | 'human_led'
  | 'agent_led_approval_gated'
  | 'autonomous_delivery';

export interface MilestoneSnapshot {
  milestone_id: string;
  key: string;
  title: string;
  sequence: number;
  status: string;
  successor_id?: string | null;
  active_autonomy_mode: MilestoneAutonomyMode;
  completed_at?: string | null;
}

export interface MilestoneReadinessSnapshot {
  assessment_id: string;
  completed_milestone_id: string;
  next_milestone_id?: string | null;
  status: string;
  evidence_snapshot: Record<string, unknown>;
  rubric: Record<string, unknown>;
  reviewer_narrative?: string | null;
  recommended_mode?: MilestoneAutonomyMode | null;
  approved_mode?: MilestoneAutonomyMode | null;
  decision_reason?: string | null;
}
