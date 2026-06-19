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

    fireEvent.click(screen.getByRole('button', { name: 'Queue dashboard task' }));

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
  });

  it('validates task text before submitting', () => {
    const { container } = renderPage();

    const form = container.querySelector('form');
    expect(form).not.toBeNull();
    fireEvent.submit(form as HTMLFormElement);

    expect(screen.getByRole('alert')).toHaveTextContent('Task text is required.');
    expect(api.submitTask).not.toHaveBeenCalled();
  });

  it('shows task submission loading state', async () => {
    vi.mocked(api.submitTask).mockReturnValue(new Promise(() => {}));
    renderPage();

    fireEvent.change(screen.getByLabelText('Task text'), {
      target: { value: 'Investigate queue behavior' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Queue dashboard task' }));

    expect(await screen.findByText('Queueing...')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Queue dashboard task' })).toBeDisabled();
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
    fireEvent.click(screen.getByRole('button', { name: 'Trigger configured scout run' }));

    await waitFor(() => expect(api.triggerScoutTask).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('Scout queued')).toBeInTheDocument();
    expect(screen.getByText('task-scout')).toBeInTheDocument();
  });

  it('ignores duplicate scout triggers while the mutation is pending', async () => {
    vi.mocked(api.triggerScoutTask).mockReturnValue(new Promise(() => {}));
    renderPage();

    fireEvent.click(screen.getByRole('tab', { name: /Scout/i }));
    const scoutButton = screen.getByRole('button', { name: 'Trigger configured scout run' });

    fireEvent.click(scoutButton);
    await screen.findByText('Triggering...');
    fireEvent.click(scoutButton);

    expect(api.triggerScoutTask).toHaveBeenCalledTimes(1);
  });

  it('renders scout trigger errors', async () => {
    vi.mocked(api.triggerScoutTask).mockRejectedValue(new Error('Scout repo is not configured'));
    renderPage();

    fireEvent.click(screen.getByRole('tab', { name: /Scout/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Trigger configured scout run' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Scout repo is not configured');
  });
});
