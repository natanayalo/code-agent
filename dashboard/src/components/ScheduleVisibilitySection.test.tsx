import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { api } from '../services/api';
import { DependencyReviewScheduleSnapshot, ScheduledTaskSnapshot } from '../types/metrics';
import { TaskStatus } from '../types/task';
import { ScheduleVisibilitySection } from './ScheduleVisibilitySection';

vi.mock('../services/api', () => ({ api: {
  listDependencyReviewSchedules: vi.fn(),
  listScheduledTasks: vi.fn(),
  manageSchedule: vi.fn(),
} }));

const schedule: DependencyReviewScheduleSnapshot = {
  id: 'project',
  spec: { version: 1, purpose: 'dependency_review', repo_key: 'project', branch: 'main', weekdays: ['monday'], time: '09:00', timezone: 'Asia/Jerusalem' },
  paused: true,
  next_executions: ['2026-11-02T07:00:00Z'],
  recent_actions: [{ occurrence_id: 'occ-1', scheduled_at: '2026-10-26T07:00:00Z', started_at: null, outcome: 'history_expired' }],
  skipped_overlap: 1,
  missed_catchup: 2,
};

function mount(onOpenTask = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return { ...render(<QueryClientProvider client={client}><MemoryRouter><ScheduleVisibilitySection onOpenTask={onOpenTask} /></MemoryRouter></QueryClientProvider>), onOpenTask, client };
}

describe('ScheduleVisibilitySection', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows loading and schedule-service errors', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockReturnValueOnce(new Promise(() => {}));
    mount();
    expect(screen.getByText('Loading schedules…')).toBeInTheDocument();
    vi.mocked(api.listDependencyReviewSchedules).mockRejectedValueOnce(new Error('Temporal unavailable'));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter><ScheduleVisibilitySection onOpenTask={vi.fn()} /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByRole('alert')).toHaveTextContent('Temporal unavailable');
  });

  it('shows an empty schedule result', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([]);
    mount();
    expect(await screen.findByText('No dependency review schedules are configured.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Create a schedule in Triggers' })).toHaveAttribute('href', '/triggers?tab=schedule');
    expect(screen.queryByLabelText('Schedule ID')).not.toBeInTheDocument();
  });

  it('shows schedule details, expired history, pagination, and opens a submitted task', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([schedule]);
    const task = { task_id: 'task-1', session_id: 'session-1', status: TaskStatus.COMPLETED, task_text: 'Review dependencies', created_at: '2026-10-26T07:01:00Z', updated_at: '2026-10-26T07:02:00Z', priority: 0 };
    const rows: ScheduledTaskSnapshot[] = Array.from({ length: 20 }, (_, index) => ({ task: { ...task, task_id: `task-${index}` }, status_url: '', timeline_url: '', artifacts_url: '' }));
    vi.mocked(api.listScheduledTasks).mockResolvedValue(rows);
    const onOpenTask = vi.fn();
    mount(onOpenTask);
    expect(await screen.findByText('Paused · Task history last checked:', { exact: false })).toBeInTheDocument();
    expect(screen.getByText('monday at 09:00 (Asia/Jerusalem)')).toBeInTheDocument();
    expect(screen.getByText('history_expired')).toBeInTheDocument();
    expect(screen.getByText(/Skipped overlaps: 1 · missed catch-up window: 2/)).toBeInTheDocument();
    fireEvent.click(await screen.findAllByRole('button', { name: 'Open task' }).then((buttons) => buttons[0]));
    expect(onOpenTask).toHaveBeenCalledWith('task-0');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await screen.findByText('Tasks 21–40')).toBeInTheDocument();
    expect(api.listScheduledTasks).toHaveBeenLastCalledWith('project', 20, 20);
  });

  it('keeps schedule details visible when task persistence is unavailable', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([schedule]);
    vi.mocked(api.listScheduledTasks).mockRejectedValue(new Error('history expired'));
    mount();
    expect(await screen.findByText(/Task history unavailable: history expired/)).toBeInTheDocument();
    expect(screen.getByText('Repository key', { selector: 'dt' })).toBeInTheDocument();
  });

  it('shows empty temporal history, null branch, skipped actions, and empty task results', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([{ ...schedule, paused: false, spec: { ...schedule.spec, branch: null }, next_executions: [], recent_actions: [{ occurrence_id: 'occ-2', scheduled_at: 'invalid-date', started_at: null, skipped: true, reason: 'overlap' }] }]);
    vi.mocked(api.listScheduledTasks).mockResolvedValue([]);
    mount();
    expect(await screen.findByText(/Active · Task history last checked:/)).toBeInTheDocument();
    expect(screen.getByText('Default branch')).toBeInTheDocument();
    expect(screen.getByText('No upcoming executions returned.')).toBeInTheDocument();
    expect(screen.getByText('skipped')).toBeInTheDocument();
    expect(screen.getByText('Scheduled: Unknown · started: Not started')).toBeInTheDocument();
    expect(screen.getByText('overlap')).toBeInTheDocument();
    expect(await screen.findByText('No submitted tasks found.')).toBeInTheDocument();
  });

  it('shows missing temporal history and loading run history', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([{ ...schedule, recent_actions: [], next_executions: [] }]);
    vi.mocked(api.listScheduledTasks).mockReturnValue(new Promise(() => {}));
    mount();
    expect(await screen.findByText('No recent outcomes retained by Temporal.')).toBeInTheDocument();
    expect(screen.getByText('Loading submitted tasks…')).toBeInTheDocument();
  });

  it('keeps Previous available when an exact page-size result advances to an empty page', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([schedule]);
    const task = { task_id: 'task-1', session_id: 'session-1', status: TaskStatus.COMPLETED, task_text: 'Review dependencies', created_at: '2026-10-26T07:01:00Z', updated_at: '2026-10-26T07:02:00Z', priority: 0 };
    const page = Array.from({ length: 20 }, (_, index) => ({ task: { ...task, task_id: `task-${index}` }, status_url: '', timeline_url: '', artifacts_url: '' }));
    vi.mocked(api.listScheduledTasks).mockImplementation((_id, _limit, offset) => Promise.resolve(offset === 0 ? page : []));
    mount();
    expect(await screen.findByText('Tasks 1–20')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await screen.findByText('No more submitted tasks.')).toBeInTheDocument();
    expect(screen.getByText('Page 2')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Previous' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
    expect(await screen.findByText('Tasks 1–20')).toBeInTheDocument();
  });

  it('keeps Previous available after a later page request fails', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([schedule]);
    const task = { task_id: 'task-1', session_id: 'session-1', status: TaskStatus.COMPLETED, task_text: 'Review dependencies', created_at: '2026-10-26T07:01:00Z', updated_at: '2026-10-26T07:02:00Z', priority: 0 };
    const page = Array.from({ length: 20 }, (_, index) => ({ task: { ...task, task_id: `task-${index}` }, status_url: '', timeline_url: '', artifacts_url: '' }));
    vi.mocked(api.listScheduledTasks).mockImplementation((_id, _limit, offset) => offset === 0 ? Promise.resolve(page) : Promise.reject(new Error('page request failed')));
    mount();
    expect(await screen.findByText('Tasks 1–20')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('page request failed');
    expect(screen.getByRole('button', { name: 'Previous' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
    expect(await screen.findByText('Tasks 1–20')).toBeInTheDocument();
  });
  it('resumes, pauses, and triggers a paused schedule with returned state and refreshed history', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([schedule]);
    vi.mocked(api.listScheduledTasks).mockResolvedValue([]);
    let resolveOperation!: (value: DependencyReviewScheduleSnapshot) => void;
    vi.mocked(api.manageSchedule).mockReturnValueOnce(new Promise((resolve) => { resolveOperation = resolve; }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Trigger now' })).toBeDisabled());
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([{ ...schedule, paused: false }]);
    resolveOperation({ ...schedule, paused: false });
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeEnabled();
    expect(api.manageSchedule).toHaveBeenCalledWith('project', 'resume');
    expect(api.listScheduledTasks).toHaveBeenCalledTimes(2);
    vi.mocked(api.manageSchedule).mockResolvedValueOnce(schedule);
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([schedule]);
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeEnabled();
    expect(api.manageSchedule).toHaveBeenCalledWith('project', 'pause');
    vi.mocked(api.manageSchedule).mockResolvedValueOnce(schedule);
    fireEvent.click(screen.getByRole('button', { name: 'Trigger now' }));
    await waitFor(() => expect(api.manageSchedule).toHaveBeenCalledWith('project', 'trigger'));
    expect(await screen.findByText(/trigger request completed/)).toBeInTheDocument();
    expect(screen.getByText(/manual trigger can run while the schedule is paused/i)).toBeInTheDocument();
  });

  it('retains context and blocks actions until both refreshes recover after an unknown outcome', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([schedule]);
    vi.mocked(api.listScheduledTasks).mockResolvedValue([]);
    vi.mocked(api.manageSchedule).mockRejectedValueOnce(new Error('Temporal unavailable'));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Trigger now' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('outcome is unknown');
    expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled();
    vi.mocked(api.listScheduledTasks).mockRejectedValueOnce(new Error('offline'));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh schedule and runs' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Refresh failed');
    expect(screen.getByText('Repository key', { selector: 'dt' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Trigger now' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh schedule and runs' }));
    expect(await screen.findByRole('status')).toHaveTextContent('inspect recent outcomes');
    expect(screen.getByRole('button', { name: 'Trigger now' })).toBeEnabled();
  });

  it('keeps the returned state and requires recovery if the post-action refresh fails', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValueOnce([schedule]).mockRejectedValueOnce(new Error('offline'));
    vi.mocked(api.listScheduledTasks).mockResolvedValue([]);
    vi.mocked(api.manageSchedule).mockResolvedValue({ ...schedule, paused: false });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('request completed, but refresh failed');
    expect(screen.getByRole('button', { name: 'Pause' })).toBeDisabled();
  });

  it('does not overwrite another schedule when overlapping refreshes finish out of order', async () => {
    const other = { ...schedule, id: 'other' };
    const active = { ...other, paused: false };
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValueOnce([schedule, other]);
    let finishFirstRuns!: (value: ScheduledTaskSnapshot[]) => void;
    let projectRuns = 0;
    vi.mocked(api.listScheduledTasks).mockImplementation((id) => id === 'project' && ++projectRuns > 1
      ? new Promise((resolve) => { finishFirstRuns = resolve; }) : Promise.resolve([]));
    vi.mocked(api.manageSchedule).mockImplementation((id) => Promise.resolve(id === 'other' ? active : schedule));
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValueOnce([schedule, other]).mockResolvedValueOnce([schedule, active]);
    mount();
    const firstCard = (await screen.findByRole('heading', { name: 'dependency review · project' })).closest('article')!;
    const secondCard = screen.getByRole('heading', { name: 'dependency review · other' }).closest('article')!;
    fireEvent.click(within(firstCard).getByRole('button', { name: 'Trigger now' }));
    await waitFor(() => expect(finishFirstRuns).toBeDefined());
    fireEvent.click(within(secondCard).getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(within(secondCard).getByRole('button', { name: 'Pause' })).toBeEnabled());
    await act(async () => finishFirstRuns([]));
    expect(within(secondCard).getByRole('button', { name: 'Pause' })).toBeEnabled();
  });

  it('retains an unknown outcome across navigation until recovery succeeds', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([schedule]);
    vi.mocked(api.listScheduledTasks).mockResolvedValue([]);
    vi.mocked(api.manageSchedule).mockRejectedValueOnce(new Error('response lost'));
    const { unmount, client } = mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Trigger now' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('outcome is unknown');
    unmount();
    render(<QueryClientProvider client={client}><MemoryRouter><ScheduleVisibilitySection onOpenTask={vi.fn()} /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByRole('alert')).toHaveTextContent('outcome is unknown');
    expect(screen.getByRole('button', { name: 'Trigger now' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh schedule and runs' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Trigger now' })).toBeEnabled());
  });

  it('does not apply a stale card refresh after a newer list arrives during run history loading', async () => {
    let finishRuns!: (value: ScheduledTaskSnapshot[]) => void;
    let runsCount = 0;
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([schedule]);
    vi.mocked(api.listScheduledTasks).mockImplementation(() => ++runsCount === 1
      ? Promise.resolve([]) : new Promise((resolve) => { finishRuns = resolve; }));
    vi.mocked(api.manageSchedule).mockResolvedValue(schedule);
    const { client } = mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Trigger now' }));
    await waitFor(() => expect(finishRuns).toBeDefined());
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([{ ...schedule, paused: false }]);
    await act(async () => { await client.refetchQueries({ queryKey: ['dependency-review-schedules'] }); });
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeDisabled();
    await act(async () => finishRuns([]));
    expect(screen.getByRole('button', { name: 'Pause' })).toBeDisabled();
    expect(await screen.findByRole('alert')).toHaveTextContent('refresh failed');
  });

  it('discards a list request that began before an operation completed', async () => {
    let finishOldList!: (value: DependencyReviewScheduleSnapshot[]) => void;
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValueOnce([schedule])
      .mockReturnValueOnce(new Promise((resolve) => { finishOldList = resolve; }))
      .mockResolvedValue([{ ...schedule, paused: false }]);
    vi.mocked(api.listScheduledTasks).mockResolvedValue([]);
    let finishOperation!: (value: DependencyReviewScheduleSnapshot) => void;
    vi.mocked(api.manageSchedule).mockReturnValue(new Promise((resolve) => { finishOperation = resolve; }));
    const { client } = mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));
    // Start a background list request before the operation response is applied.
    await act(async () => { void client.refetchQueries({ queryKey: ['dependency-review-schedules'] }); });
    await waitFor(() => expect(finishOldList).toBeDefined());
    await act(async () => finishOperation({ ...schedule, paused: false }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled());
    await act(async () => finishOldList([schedule]));
    expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled();
  });

  it('does not apply an operation response older than an already updated list', async () => {
    let finishOperation!: (value: DependencyReviewScheduleSnapshot) => void;
    let finishRuns!: (value: ScheduledTaskSnapshot[]) => void;
    let runsCount = 0;
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([schedule]);
    vi.mocked(api.listScheduledTasks).mockImplementation(() => ++runsCount === 1
      ? Promise.resolve([]) : new Promise((resolve) => { finishRuns = resolve; }));
    vi.mocked(api.manageSchedule).mockReturnValue(new Promise((resolve) => { finishOperation = resolve; }));
    const { client } = mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Trigger now' }));
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([{ ...schedule, paused: false }]);
    await act(async () => { await client.refetchQueries({ queryKey: ['dependency-review-schedules'] }); });
    await act(async () => finishOperation(schedule));
    await waitFor(() => expect(finishRuns).toBeDefined());
    expect(screen.getByRole('button', { name: 'Pause' })).toBeDisabled();
    await act(async () => finishRuns([]));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled());
  });

  it('shows a failed background refresh and blocks controls until the list is refreshed', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValueOnce([schedule]).mockRejectedValueOnce(new Error('offline'));
    vi.mocked(api.listScheduledTasks).mockResolvedValue([]);
    const { client } = mount();
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeEnabled();
    await act(async () => { await client.refetchQueries({ queryKey: ['dependency-review-schedules'] }); });
    expect(await screen.findByRole('alert')).toHaveTextContent('Schedule refresh failed: offline');
    expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled();
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([{ ...schedule, paused: false }]);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh schedules' }));
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeEnabled();
  });

  it('does not clear a list refresh error when a single card is recovered', async () => {
    vi.mocked(api.listDependencyReviewSchedules).mockResolvedValue([schedule]);
    vi.mocked(api.listScheduledTasks).mockResolvedValue([]);
    vi.mocked(api.manageSchedule).mockRejectedValueOnce(new Error('response lost'));
    const { client } = mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Trigger now' }));
    expect(await screen.findByText(/outcome is unknown/)).toBeInTheDocument();
    vi.mocked(api.listDependencyReviewSchedules).mockRejectedValueOnce(new Error('offline'));
    await act(async () => { await client.refetchQueries({ queryKey: ['dependency-review-schedules'] }); });
    expect(await screen.findByText(/Schedule refresh failed: offline/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh schedule and runs' }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Schedule and runs refreshed'));
    expect(screen.getByText(/Schedule refresh failed: offline/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled();
  });
});
