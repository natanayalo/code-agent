import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';
import { DependencyReviewScheduleSnapshot } from '../types/metrics';
import { Link } from 'react-router-dom';

const PAGE_SIZE = 20;

function date(value: string | null | undefined): string {
  if (!value) return 'Not started';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Unknown' : parsed.toLocaleString();
}

function ScheduleCard({ schedule, onOpenTask }: { schedule: DependencyReviewScheduleSnapshot; onOpenTask: (taskId: string) => void }) {
  const [offset, setOffset] = React.useState(0);
  const [busy, setBusy] = React.useState(false);
  const [needsRefresh, setNeedsRefresh] = React.useState(false);
  const [message, setMessage] = React.useState('');
  const client = useQueryClient();
  const runs = useQuery({
    queryKey: ['scheduled-tasks', schedule.id, offset],
    queryFn: () => api.listScheduledTasks(schedule.id, PAGE_SIZE, offset),
    refetchInterval: 60000,
  });
  const refresh = async (): Promise<boolean> => {
    const [schedulesResult, runsResult] = await Promise.allSettled([
      api.listDependencyReviewSchedules(),
      api.listScheduledTasks(schedule.id, PAGE_SIZE, 0),
    ]);
    if (schedulesResult.status === 'fulfilled' && schedulesResult.value.some((item) => item.id === schedule.id)) {
      client.setQueryData(['dependency-review-schedules'], schedulesResult.value);
    }
    if (runsResult.status === 'fulfilled') {
      client.setQueryData(['scheduled-tasks', schedule.id, 0], runsResult.value);
      setOffset(0);
    }
    return schedulesResult.status === 'fulfilled' &&
      schedulesResult.value.some((item) => item.id === schedule.id) && runsResult.status === 'fulfilled';
  };

  const recover = async () => {
    setBusy(true);
    const refreshed = await refresh();
    setNeedsRefresh(!refreshed);
    setMessage(refreshed
      ? 'Schedule and runs refreshed. If a trigger outcome was unknown, inspect recent outcomes and submitted tasks before retrying.'
      : 'Refresh failed. Schedule actions remain disabled; try refreshing again.');
    setBusy(false);
  };

  const manage = async (operation: 'pause' | 'resume' | 'trigger') => {
    setBusy(true);
    setMessage('');
    try {
      const result = await api.manageSchedule(schedule.id, operation);
      client.setQueryData<DependencyReviewScheduleSnapshot[]>(['dependency-review-schedules'], (current) =>
        current?.map((item) => item.id === result.id ? result : item));
      const refreshed = await refresh();
      setNeedsRefresh(!refreshed);
      setMessage(refreshed
        ? `${operation} request completed. Schedule and runs refreshed.`
        : `${operation} request completed, but refresh failed. Refresh before another action.`);
    } catch (error) {
      setNeedsRefresh(true);
      setMessage(`${operation} outcome is unknown and may have succeeded: ${(error as Error).message}. Refresh and inspect recent outcomes and submitted tasks before retrying.`);
    } finally {
      setBusy(false);
    }
  };
  const spec = schedule.spec;
  return <article className="metric-detail-card card schedule-visibility-card">
    <h3>{schedule.spec.purpose.replaceAll('_', ' ')} · {schedule.id}</h3>
    <p className="metric-note">{schedule.paused ? 'Paused' : 'Active'} · Task history last checked: {date(runs.dataUpdatedAt ? new Date(runs.dataUpdatedAt).toISOString() : null)}</p>
    <div className="schedule-controls" aria-label={`Manage schedule ${schedule.id}`}>
      <button className="health-refresh-button" disabled={busy || needsRefresh} onClick={() => void manage(schedule.paused ? 'resume' : 'pause')}>{schedule.paused ? 'Resume' : 'Pause'}</button>
      <button className="health-refresh-button" disabled={busy || needsRefresh} onClick={() => void manage('trigger')}>Trigger now</button>
      {needsRefresh ? <button className="health-refresh-button" disabled={busy} onClick={() => void recover()}>Refresh schedule and runs</button> : null}
    </div>
    <p className="metric-note">A manual trigger can run while the schedule is paused.</p>
    {message ? <p role={needsRefresh ? 'alert' : 'status'}>{message}</p> : null}
    <dl className="schedule-spec">
      <div><dt>Repository key</dt><dd>{spec.repo_key}</dd></div>
      <div><dt>Branch</dt><dd>{spec.branch || 'Default branch'}</dd></div>
      <div><dt>Schedule</dt><dd>{spec.weekdays.join(', ')} at {spec.time} ({spec.timezone})</dd></div>
      {spec.evidence ? <div><dt>Evidence</dt><dd>{spec.evidence.source} · {spec.evidence.lookback_hours} hours</dd></div> : null}
    </dl>
    <h4>Upcoming executions</h4>
    {schedule.next_executions.length ? <ul>{schedule.next_executions.map((item) => <li key={item}>{date(item)}</li>)}</ul> : <p>No upcoming executions returned.</p>}
    <h4>Recent outcomes</h4>
    {schedule.recent_actions.length ? <ul className="ops-list">{schedule.recent_actions.map((action) => <li key={action.occurrence_id}>
      <strong>{action.outcome ?? (action.skipped ? 'skipped' : 'No outcome available')}</strong>
      <span>Scheduled: {date(action.scheduled_at)} · started: {date(action.started_at)}</span>
      {action.reason ? <small>{action.reason}</small> : null}
    </li>)}</ul> : <p>No recent outcomes retained by Temporal.</p>}
    <p className="metric-note">Skipped overlaps: {schedule.skipped_overlap} · missed catch-up window: {schedule.missed_catchup}</p>
    <h4>Submitted tasks</h4>
    {runs.isLoading ? <p>Loading submitted tasks…</p> : runs.error ? <p role="alert">Task history unavailable: {(runs.error as Error).message}. Temporal schedule details above remain available.</p> : null}
    {!runs.isLoading && !runs.error && runs.data?.length ? <ul className="ops-list">{runs.data.map(({ task }) => <li key={task.task_id}>
        <strong>{task.status}</strong><span>{task.task_text}</span><small>{task.task_id} · {date(task.created_at)}</small>
        <button className="btn-secondary" onClick={() => onOpenTask(task.task_id)}>Open task</button>
      </li>)}</ul> : null}
    {!runs.isLoading && !runs.error && !runs.data?.length ? <p>{offset ? 'No more submitted tasks.' : 'No submitted tasks found.'}</p> : null}
    {offset > 0 || (runs.data?.length ?? 0) > 0 ?
      <div className="schedule-pagination">
        <button className="btn-secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
        <span>{runs.data?.length ? `Tasks ${offset + 1}–${offset + runs.data.length}` : `Page ${offset / PAGE_SIZE + 1}`}</span>
        <button className="btn-secondary" disabled={runs.isLoading || Boolean(runs.error) || (runs.data?.length ?? 0) < PAGE_SIZE} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
      </div>
      : null}
  </article>;
}

export function ScheduleVisibilitySection({ onOpenTask }: { onOpenTask: (taskId: string) => void }) {
  const schedules = useQuery({ queryKey: ['dependency-review-schedules'], queryFn: api.listDependencyReviewSchedules, refetchInterval: 60000 });
  return <section className="metrics-page" aria-labelledby="schedule-visibility-heading">
    <div className="page-header"><h2 id="schedule-visibility-heading">Read-only schedules</h2><p className="page-subtitle">Schedule configuration and execution history</p></div>
    <p className="metric-note"><Link className="schedule-navigation-link" to="/triggers?tab=schedule">Create a schedule in Triggers</Link></p>
    {schedules.isLoading ? <p>Loading schedules…</p> : schedules.error && !schedules.data ? <p role="alert">Schedule data unavailable: {(schedules.error as Error).message}</p> : <>
      <p className="metric-note">Schedule list last checked: {date(schedules.dataUpdatedAt ? new Date(schedules.dataUpdatedAt).toISOString() : null)}</p>
      {schedules.data?.length ? <div className="metrics-details-grid">{schedules.data.map((schedule) => <ScheduleCard key={schedule.id} schedule={schedule} onOpenTask={onOpenTask} />)}</div> : <p>No dependency review schedules are configured.</p>}
    </>}
  </section>;
}
