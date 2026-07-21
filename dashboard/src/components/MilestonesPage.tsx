import React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';
import { MilestoneAutonomyMode } from '../types/milestone';
import { DashboardLayout } from './layout/DashboardLayout';

export function MilestonesPage() {
  const queryClient = useQueryClient();
  const milestones = useQuery({ queryKey: ['milestones'], queryFn: () => api.listMilestones() });
  const assessments = useQuery({
    queryKey: ['milestone-readiness-assessments'],
    queryFn: () => api.listMilestoneReadinessAssessments(),
  });
  const decide = useMutation({
    mutationFn: ({ id, approved, mode }: { id: string; approved: boolean; mode?: MilestoneAutonomyMode }) =>
      api.decideMilestoneReadiness(id, approved, mode),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['milestone-readiness-assessments'] }),
  });
  const pending = assessments.data?.filter((item) => item.status === 'pending_approval') ?? [];

  return <DashboardLayout><main className="dashboard-content"><div className="dashboard-content-inner">
    <section className="panel"><h2>Milestone readiness</h2>
      <p>Recommendations are advisory. Approval changes only the successor’s bounded operating mode.</p>
      {pending.map((item) => <article key={item.assessment_id} className="task-card">
        <h3>Readiness assessment</h3><p>{item.reviewer_narrative}</p>
        <pre>{JSON.stringify(item.rubric, null, 2)}</pre>
        <button onClick={() => decide.mutate({ id: item.assessment_id, approved: true, mode: item.recommended_mode ?? undefined })}>Approve {item.recommended_mode}</button>
        <button onClick={() => decide.mutate({ id: item.assessment_id, approved: false })}>Reject</button>
      </article>)}
      {pending.length === 0 ? <p>No readiness decisions are pending.</p> : null}
    </section>
    <section className="panel"><h2>Tracked milestones</h2>
      {milestones.data?.map((item) => <div key={item.milestone_id} className="task-card">
        <strong>{item.key}: {item.title}</strong><p>{item.status} · {item.active_autonomy_mode}</p>
      </div>)}
    </section>
  </div></main></DashboardLayout>;
}
