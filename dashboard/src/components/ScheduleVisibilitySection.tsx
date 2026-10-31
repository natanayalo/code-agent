import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';
import { DependencyReviewScheduleSnapshot } from '../types/metrics';
import { Link } from 'react-router-dom';

const PAGE_SIZE = 20;
const SCHEDULES_KEY = ['dependency-review-schedules'];
type ControlState = { busy: boolean; needsRefresh: boolean; message: string };
const INITIAL_CONTROL_STATE: ControlState = { busy: false, needsRefresh: false, message: '' };

function date(value: string | null | undefined): string {
  if (!value) return 'Not started';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Unknown' : parsed.toLocaleString();
}

function ScheduleCard({ schedule, onOpenTask, listUnavailable }: { schedule: DependencyReviewScheduleSnapshot; onOpenTask: (taskId: string) => void; listUnavailable: boolean }) {
  const [offset, setOffset] = React.useState(0);
  const client = useQueryClient();
  const controlKey = ['schedule-control', schedule.id];
  const { data: control } = useQuery({
    queryKey: controlKey, queryFn: () => INITIAL_CONTROL_STATE,
    initialData: INITIAL_CONTROL_STATE, enabled: false, gcTime: Infinity,
  });
  const { busy, needsRefresh, message } = control;
  const updateControl = (changes: Partial<ControlState>) => {
    client.setQueryData<ControlState>(controlKey, (current) => ({ ...INITIAL_CONTROL_STATE, ...current, ...changes }));
  };
  const updateSchedule = async (updated: DependencyReviewScheduleSnapshot, expectedVersion: number | undefined): Promise<boolean> => {
    // Reject snapshots superseded by any shared list update while the request was in flight.
    if (client.getQueryState(SCHEDULES_KEY)?.dataUpdateCount !== expectedVersion) return false;
    // A list request started before this update must not restore an older state.
    await client.cancelQueries({ queryKey: SCHEDULES_KEY, exact: true });
    const query = client.getQueryCache().find<DependencyReviewScheduleSnapshot[]>({ queryKey: SCHEDULES_KEY, exact: true });
    if (query?.state.dataUpdateCount !== expectedVersion) return false;
    if (query?.state.status === 'error') {
      // Updating one card does not repair a failed refresh of the whole list.
      query.setState({ data: query.state.data?.map((item) => item.id === updated.id ? updated : item), dataUpdateCount: query.state.dataUpdateCount + 1 });
      return true;
    }
    client.setQueryData<DependencyReviewScheduleSnapshot[]>(SCHEDULES_KEY, (current) =>
      current?.map((item) => item.id === updated.id ? updated : item));
    return true;
  };
  const runs = useQuery({
    queryKey: ['scheduled-tasks', schedule.id, offset],
    queryFn: () => api.listScheduledTasks(schedule.id, PAGE_SIZE, offset),
    refetchInterval: 60000,
  });
  const refresh = async (): Promise<boolean> => {
    const version = client.getQueryState(SCHEDULES_KEY)?.dataUpdateCount;
    const runsKey = ['scheduled-tasks', schedule.id, 0];
    const runsVersion = client.getQueryState(runsKey)?.dataUpdateCount;
    const [schedulesResult, runsResult] = await Promise.allSettled([
      api.listDependencyReviewSchedules(),
      api.listScheduledTasks(schedule.id, PAGE_SIZE, 0),
    ]);
    const updated = schedulesResult.status === 'fulfilled'
      ? schedulesResult.value.find((item) => item.id === schedule.id) : undefined;
    const applied = updated ? await updateSchedule(updated, version) : false;
    let runsApplied = false;
    // Preserve newer history and prevent an older in-flight query from replacing recovery evidence.
    if (runsResult.status === 'fulfilled' && client.getQueryState(runsKey)?.dataUpdateCount === runsVersion) {
      await client.cancelQueries({ queryKey: runsKey, exact: true });
      if (client.getQueryState(runsKey)?.dataUpdateCount === runsVersion) {
        client.setQueryData(runsKey, runsResult.value);
        setOffset(0);
        runsApplied = true;
      }
    }
    return applied && runsApplied;
  };

  const recover = async () => {
    if (client.getQueryData<ControlState>(controlKey)?.busy) return;
    updateControl({ busy: true });
    const refreshed = await refresh();
    updateControl({ busy: false, needsRefresh: !refreshed, message: refreshed
      ? 'Schedule and runs refreshed. If a trigger outcome was unknown, inspect recent outcomes and submitted tasks before retrying.'
      : 'Refresh failed. Schedule actions remain disabled; try refreshing again.' });
  };

  const manage = async (operation: 'pause' | 'resume' | 'trigger') => {
    const current = client.getQueryData<ControlState>(controlKey);
    if (current?.busy || current?.needsRefresh || listUnavailable) return;
    updateControl({ busy: true, message: '' });
    const version = client.getQueryState(SCHEDULES_KEY)?.dataUpdateCount;
    try {
      const result = await api.manageSchedule(schedule.id, operation);
      await updateSchedule(result, version);
      const refreshed = await refresh();
      updateControl({ needsRefresh: !refreshed, message: refreshed
        ? `${operation} request completed. Schedule and runs refreshed.`
        : `${operation} request completed, but refresh failed. Refresh before another action.` });
    } catch (error) {
      updateControl({ needsRefresh: true, message: `${operation} outcome is unknown and may have succeeded: ${(error as Error).message}. Refresh and inspect recent outcomes and submitted tasks before retrying.` });
    } finally {
      updateControl({ busy: false });
    }
  };
  const spec = schedule.spec;
  return <article className="metric-detail-card card schedule-visibility-card">
    <h3>{schedule.spec.purpose.replaceAll('_', ' ')} · {schedule.id}</h3>
    <p className="metric-note">{schedule.paused ? 'Paused' : 'Active'} · Task history last checked: {date(runs.dataUpdatedAt ? new Date(runs.dataUpdatedAt).toISOString() : null)}</p>
    <div className="schedule-controls" aria-label={`Manage schedule ${schedule.id}`}>
      <button className="health-refresh-button" disabled={busy || needsRefresh || listUnavailable} onClick={() => void manage(schedule.paused ? 'resume' : 'pause')}>{schedule.paused ? 'Resume' : 'Pause'}</button>
      <button className="health-refresh-button" disabled={busy || needsRefresh || listUnavailable} onClick={() => void manage('trigger')}>Trigger now</button>
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
  const schedules = useQuery({ queryKey: SCHEDULES_KEY, queryFn: api.listDependencyReviewSchedules, refetchInterval: 60000 });
  return <section className="metrics-page" aria-labelledby="schedule-visibility-heading">
    <div className="page-header"><h2 id="schedule-visibility-heading">Read-only schedules</h2><p className="page-subtitle">Schedule configuration and execution history</p></div>
    <p className="metric-note"><Link className="schedule-navigation-link" to="/triggers?tab=schedule">Create a schedule in Triggers</Link></p>
    {schedules.isLoading ? <p>Loading schedules…</p> : schedules.error && !schedules.data ? <p role="alert">Schedule data unavailable: {(schedules.error as Error).message}</p> : <>
      {schedules.error ? <p role="alert">Schedule refresh failed: {(schedules.error as Error).message}. Actions are disabled until the list refreshes. <button className="health-refresh-button" onClick={() => void schedules.refetch()}>Refresh schedules</button></p> : null}
      <p className="metric-note">Schedule list last checked: {date(schedules.dataUpdatedAt ? new Date(schedules.dataUpdatedAt).toISOString() : null)}</p>
      {schedules.data?.length ? <div className="metrics-details-grid">{schedules.data.map((schedule) => <ScheduleCard key={schedule.id} schedule={schedule} onOpenTask={onOpenTask} listUnavailable={Boolean(schedules.error)} />)}</div> : <p>No dependency review schedules are configured.</p>}
    </>}
  </section>;
}
