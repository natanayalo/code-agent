import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TriggerActionsPage } from './TriggerActionsPage';
import { api } from '../services/api';
import { TaskSnapshot, TaskStatus } from '../types/task';

vi.mock('../services/api', () => ({
  api: {
    submitTask: vi.fn(),
    triggerScoutTask: vi.fn(),
  },
}));

const now = '2026-06-19T10:00:00.000Z';

const createTaskSnapshot = (overrides: Partial<TaskSnapshot> = {}): TaskSnapshot => ({
  task_id: 'task-new',
  session_id: 'session-1',
  status: TaskStatus.PENDING,
  task_text: 'Queued from dashboard',
  priority: 0,
  created_at: now,
  updated_at: now,
  timeline: [],
  ...overrides,
});

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TriggerActionsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('TriggerActionsPage', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('submits a dashboard task payload and shows the queued task id', async () => {
    vi.mocked(api.submitTask).mockResolvedValue(createTaskSnapshot({ task_id: 'task-123' }));
    renderPage();

    const liveRegion = screen.getByRole('status');
    expect(liveRegion).toBeEmptyDOMElement();

    fireEvent.change(screen.getByLabelText('Task text'), {
      target: { value: '  Add a dashboard trigger tab  ' },
    });
    fireEvent.change(screen.getByLabelText('Repository URL'), {
      target: { value: '  https://github.com/example/repo  ' },
    });
    fireEvent.change(screen.getByLabelText('Branch'), { target: { value: ' main ' } });
    fireEvent.change(screen.getByLabelText('Task type'), { target: { value: 'bugfix' } });
    fireEvent.change(screen.getByLabelText('Worker'), { target: { value: 'codex' } });
    fireEvent.change(screen.getByLabelText('Priority'), { target: { value: '2' } });

    fireEvent.click(screen.getByRole('button', { name: 'Queue Task' }));

    await waitFor(() => {
      expect(api.submitTask).toHaveBeenCalledWith({
        task_text: 'Add a dashboard trigger tab',
        repo_url: 'https://github.com/example/repo',
        branch: 'main',
        priority: 2,
        worker_override: 'codex',
        constraints: {
          task_type: 'bugfix',
          trigger_source: 'dashboard',
        },
        session: {
          channel: 'dashboard',
          external_user_id: 'dashboard:operator',
          external_thread_id: 'dashboard-triggers',
          display_name: 'Dashboard Operator',
        },
      });
    });
    expect(await screen.findByText('Task queued')).toBeInTheDocument();
    expect(screen.getByText('task-123')).toBeInTheDocument();
    expect(liveRegion).toHaveTextContent('Task queued');
    expect(liveRegion).toHaveTextContent('task-123');
  });

  it('omits task priority when the priority field is empty', async () => {
    vi.mocked(api.submitTask).mockResolvedValue(createTaskSnapshot({ task_id: 'task-no-priority' }));
    renderPage();

    expect(screen.getByLabelText('Priority')).toHaveValue(null);
    fireEvent.change(screen.getByLabelText('Task text'), {
      target: { value: 'Queue task without priority' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Queue Task' }));

    await waitFor(() => expect(api.submitTask).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.submitTask).mock.calls[0][0]).not.toHaveProperty('priority');
  });

  it('rejects invalid task priority values before submitting', () => {
    const { container } = renderPage();

    fireEvent.change(screen.getByLabelText('Task text'), {
      target: { value: 'Queue task with invalid priority' },
    });
    fireEvent.change(screen.getByLabelText('Priority'), { target: { value: '-1' } });

    const form = container.querySelector('form');
    expect(form).not.toBeNull();
    fireEvent.submit(form as HTMLFormElement);

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Priority must be a whole number between 0 and 2147483647.',
    );
    expect(api.submitTask).not.toHaveBeenCalled();
  });

  it('rejects task priority values above the database integer limit', () => {
    const { container } = renderPage();

    fireEvent.change(screen.getByLabelText('Task text'), {
      target: { value: 'Queue task with huge priority' },
    });
    fireEvent.change(screen.getByLabelText('Priority'), { target: { value: '2147483648' } });

    const form = container.querySelector('form');
    expect(form).not.toBeNull();
    fireEvent.submit(form as HTMLFormElement);

    expect(screen.getByLabelText('Priority')).toHaveAttribute('max', '2147483647');
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Priority must be a whole number between 0 and 2147483647.',
    );
    expect(api.submitTask).not.toHaveBeenCalled();
  });

  it('validates task text before submitting', () => {
    const { container } = renderPage();

    const form = container.querySelector('form');
    expect(form).not.toBeNull();
    fireEvent.submit(form as HTMLFormElement);

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Task text is required.');
    expect(alert).toHaveClass('trigger-error-banner');
    expect(alert.querySelector('svg')).not.toBeNull();
    expect(api.submitTask).not.toHaveBeenCalled();
  });

  it('shows task submission loading state', async () => {
    vi.mocked(api.submitTask).mockReturnValue(new Promise(() => {}));
    renderPage();

    fireEvent.change(screen.getByLabelText('Task text'), {
      target: { value: 'Investigate queue behavior' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Queue Task' }));

    expect(await screen.findByText('Queueing...')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Queueing...' })).toBeDisabled();
  });

  it('ignores duplicate task submissions while the mutation is pending', async () => {
    vi.mocked(api.submitTask).mockReturnValue(new Promise(() => {}));
    const { container } = renderPage();

    fireEvent.change(screen.getByLabelText('Task text'), {
      target: { value: 'Investigate queue behavior' },
    });
    const form = container.querySelector('form') as HTMLFormElement;

    fireEvent.submit(form);
    await screen.findByText('Queueing...');
    fireEvent.submit(form);

    expect(api.submitTask).toHaveBeenCalledTimes(1);
  });

  it('triggers a configured scout task and shows the queued task id', async () => {
    vi.mocked(api.triggerScoutTask).mockResolvedValue(
      createTaskSnapshot({ task_id: 'task-scout' }),
    );
    renderPage();

    fireEvent.click(screen.getByRole('tab', { name: /Scout/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Trigger Scout' }));

    await waitFor(() => expect(api.triggerScoutTask).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('Scout queued')).toBeInTheDocument();
    expect(screen.getByText('task-scout')).toBeInTheDocument();
  });

  it('ignores duplicate scout triggers while the mutation is pending', async () => {
    vi.mocked(api.triggerScoutTask).mockReturnValue(new Promise(() => {}));
    renderPage();

    fireEvent.click(screen.getByRole('tab', { name: /Scout/i }));
    const scoutButton = screen.getByRole('button', { name: 'Trigger Scout' });

    fireEvent.click(scoutButton);
    await screen.findByText('Triggering...');
    fireEvent.click(scoutButton);

    expect(api.triggerScoutTask).toHaveBeenCalledTimes(1);
  });

  it('renders scout trigger errors', async () => {
    vi.mocked(api.triggerScoutTask).mockRejectedValue(new Error('Scout repo is not configured'));
    renderPage();

    fireEvent.click(screen.getByRole('tab', { name: /Scout/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Trigger Scout' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Scout repo is not configured');
    expect(alert).toHaveClass('trigger-error-banner');
  });
});
