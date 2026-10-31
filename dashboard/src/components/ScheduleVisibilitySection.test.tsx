import { fireEvent, render, screen } from '@testing-library/react';
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
  return { ...render(<QueryClientProvider client={client}><MemoryRouter><ScheduleVisibilitySection onOpenTask={onOpenTask} /></MemoryRouter></QueryClientProvider>), onOpenTask };
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
});
