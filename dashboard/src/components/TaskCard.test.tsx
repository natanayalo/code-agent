import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { TaskCard } from './TaskCard';
import { TaskStatus, ApprovalStatus, TaskSnapshot } from '../types/task';
import { api } from '../services/api';

vi.mock('../services/api', () => ({
  api: {
    decideTaskApproval: vi.fn(),
    replayTask: vi.fn(),
  },
}));

describe('TaskCard', () => {
  beforeEach(() => {
    vi.mocked(api.decideTaskApproval).mockClear();
    vi.mocked(api.replayTask).mockClear();
  });

  const mockTask = {
    task_id: 'test-task-1',
    task_text: 'Test task description',
    status: TaskStatus.IN_PROGRESS,
    created_at: new Date().toISOString(),
    repo_url: 'https://github.com/user/repo',
    branch: 'main',
    chosen_worker: 'gemini',
    latest_run_status: TaskStatus.IN_PROGRESS,
    latest_run_worker: 'gemini',
  };

  it('renders task details correctly', () => {
    const { container } = render(<TaskCard task={mockTask} />);

    expect(screen.getByText('Test task description')).toBeInTheDocument();
    expect(container.querySelector('.status-badge')).toHaveTextContent('in progress');
    expect(screen.getByText('repo')).toBeInTheDocument();
    expect(screen.getByText('main')).toBeInTheDocument();
    expect(screen.getByText('gemini')).toBeInTheDocument();
  });

  it('handles click events', () => {
    const handleClick = vi.fn();
    render(<TaskCard task={mockTask} onClick={handleClick} />);

    fireEvent.click(screen.getByText('Test task description'));
    expect(handleClick).toHaveBeenCalledTimes(1);
  });

  it('renders different status badges correctly', () => {
    const completedTask = { ...mockTask, status: TaskStatus.COMPLETED };
    const { rerender, container } = render(<TaskCard task={completedTask} />);
    expect(container.querySelector('.status-badge')).toHaveTextContent('completed');
    expect(container.querySelector('.status-badge')).toHaveClass('status-success');

    const failedTask = { ...mockTask, status: TaskStatus.FAILED };
    rerender(<TaskCard task={failedTask} />);
    expect(container.querySelector('.status-badge')).toHaveTextContent('failed');
    expect(container.querySelector('.status-badge')).toHaveClass('status-error');

    const cancelledTask = { ...mockTask, status: TaskStatus.CANCELLED };
    rerender(<TaskCard task={cancelledTask} />);
    expect(container.querySelector('.status-badge')).toHaveTextContent('cancelled');
    expect(container.querySelector('.status-badge')).toHaveClass('status-error');

    const pendingTask = { ...mockTask, status: TaskStatus.PENDING };
    rerender(<TaskCard task={pendingTask} />);
    expect(container.querySelector('.status-badge')).toHaveTextContent('pending');
    expect(container.querySelector('.status-badge')).toHaveClass('status-pending');
  });

  it('formats dates correctly for today', () => {
    const today = new Date().toISOString();
    const task = { ...mockTask, created_at: today };
    render(<TaskCard task={task} />);

    // Should contain AM or PM
    expect(screen.getByText(/AM|PM/)).toBeInTheDocument();
  });

  it('formats dates correctly for past days', () => {
    const lastYear = new Date();
    lastYear.setFullYear(lastYear.getFullYear() - 1);
    const task = { ...mockTask, created_at: lastYear.toISOString() };
    render(<TaskCard task={task} />);

    // Should contain month name (short)
    const month = lastYear.toLocaleDateString('en-US', { month: 'short' });
    expect(screen.getByText(new RegExp(month))).toBeInTheDocument();
  });

  it('handles missing or invalid dates', () => {
    // @ts-expect-error: testing null date
    const taskMissing = { ...mockTask, created_at: null };
    const { rerender } = render(<TaskCard task={taskMissing} />);
    expect(screen.getByText('N/A')).toBeInTheDocument();

    const taskInvalid = { ...mockTask, created_at: 'invalid-date' };
    rerender(<TaskCard task={taskInvalid} />);
    expect(screen.getByText('N/A')).toBeInTheDocument();
  });

  it('derives repo name from various URL formats', () => {
    const testCases = [
      { url: 'https://github.com/user/my-repo.git', expected: 'my-repo' },
      { url: 'https://github.com/', expected: 'Unknown Repo' },
      { url: 'git@github.com:user/ssh-repo.git', expected: 'ssh-repo' },
      { url: 'git@github.com:', expected: 'git@github.com' },
      { url: ':', expected: 'Unknown Repo' },
      { url: '.git', expected: 'Unknown Repo' },
      { url: 'just-a-name', expected: 'just-a-name' },
      { url: '', expected: 'Unknown Repo' },
      { url: '   ', expected: 'Unknown Repo' },
    ];

    testCases.forEach(({ url, expected }) => {
      const task = { ...mockTask, repo_url: url };
      const { unmount } = render(<TaskCard task={task} />);

      if (!url) {
        expect(screen.queryByText('Unknown Repo')).toBeNull();
      } else {
        expect(screen.getByText(expected)).toBeInTheDocument();
      }

      unmount();
    });
  });

  it('handles run status classes correctly', () => {
    const statuses = [
      { status: TaskStatus.COMPLETED, expected: 'success' },
      { status: TaskStatus.FAILED, expected: 'error' },
      { status: TaskStatus.CANCELLED, expected: 'error' },
      { status: TaskStatus.IN_PROGRESS, expected: 'running' },
      { status: 'unknown', expected: 'unknown' },
    ];

    statuses.forEach(({ status, expected }) => {
      const task = { ...mockTask, latest_run_status: status };
      const { unmount, container } = render(<TaskCard task={task} />);
      const badge = container.querySelector('.run-status');
      if (badge) {
        expect(badge).toHaveTextContent(status.replace(/_/g, ' '));
        expect(badge).toHaveClass(expected);
      }
      unmount();
    });
  });

  it('handles missing run status gracefully', () => {
    const task = { ...mockTask, latest_run_status: null };
    const { container } = render(<TaskCard task={task} />);
    expect(container.querySelector('.run-status')).toBeNull();
  });

  it('handles undefined run status gracefully', () => {
    const task = { ...mockTask, latest_run_status: undefined };
    const { container } = render(<TaskCard task={task} />);
    expect(container.querySelector('.run-status')).toBeNull();
  });

  it('provides fallback for missing approval type', () => {
    const task = { ...mockTask, approval_status: 'pending' as ApprovalStatus, approval_type: null };
    render(<TaskCard task={task} />);
    expect(screen.getByText('Approval Required')).toBeInTheDocument();
  });

  describe('Approval UI', () => {
    const approvalTask = {
      ...mockTask,
      approval_status: 'pending' as ApprovalStatus,
      approval_type: 'permission_escalation',
      approval_reason: 'Testing approval',
      latest_run_requested_permission: 'dangerous_command',
    };

    it('renders approval banner when pending', () => {
      render(<TaskCard task={approvalTask} />);
      expect(screen.getByText('permission escalation')).toBeInTheDocument();
      expect(screen.getByText('Testing approval')).toBeInTheDocument();
      expect(screen.getByText('dangerous_command')).toBeInTheDocument();
      expect(screen.getByText('Approve')).toBeInTheDocument();
      expect(screen.getByText('Reject')).toBeInTheDocument();
    });

    it('does not render approval banner when not pending', () => {
      const approvedTask = { ...approvalTask, approval_status: 'approved' as ApprovalStatus };
      render(<TaskCard task={approvedTask} />);
      expect(screen.queryByText('Approve')).toBeNull();
    });

    it('handles approval click', async () => {
      const onRefresh = vi.fn();
      vi.mocked(api.decideTaskApproval).mockResolvedValueOnce({});
      render(<TaskCard task={approvalTask} onRefresh={onRefresh} />);

      fireEvent.click(screen.getByText('Approve'));
      expect(api.decideTaskApproval).toHaveBeenCalledWith(approvalTask.task_id, true);
      // Wait for async handler
      await vi.waitFor(() => expect(onRefresh).toHaveBeenCalled());
    });

    it('handles rejection click', async () => {
      const onRefresh = vi.fn();
      vi.mocked(api.decideTaskApproval).mockResolvedValueOnce({});
      render(<TaskCard task={approvalTask} onRefresh={onRefresh} />);

      fireEvent.click(screen.getByText('Reject'));
      expect(api.decideTaskApproval).toHaveBeenCalledWith(approvalTask.task_id, false);
      await vi.waitFor(() => expect(onRefresh).toHaveBeenCalled());
    });

    it('disables buttons during processing', async () => {
      // Create a promise we can control
      let resolveApproval: (value: unknown) => void;
      const approvalPromise = new Promise((resolve) => {
        resolveApproval = resolve;
      });
      vi.mocked(api.decideTaskApproval).mockReturnValueOnce(approvalPromise);

      render(<TaskCard task={approvalTask} />);
      const approveBtn = screen.getByText('Approve').closest('button');
      const rejectBtn = screen.getByText('Reject').closest('button');

      fireEvent.click(approveBtn!);
      expect(approveBtn).toBeDisabled();
      expect(rejectBtn).toBeDisabled();

      // @ts-expect-error: resolveApproval is captured from promise constructor
      resolveApproval({});
      await vi.waitFor(() => expect(approveBtn).not.toBeDisabled());
    });

    it('displays error message when approval fails', async () => {
      vi.mocked(api.decideTaskApproval).mockRejectedValueOnce(new Error('Conflict: Task already approved'));
      render(<TaskCard task={approvalTask} />);

      fireEvent.click(screen.getByText('Approve'));

      await vi.waitFor(() => {
        expect(screen.getByText('Conflict: Task already approved')).toBeInTheDocument();
      });
    });

    it('prevents duplicate clicks while processing', async () => {
      vi.mocked(api.decideTaskApproval).mockReturnValueOnce(new Promise(() => {})); // Never resolves
      render(<TaskCard task={approvalTask} />);

      const approveBtn = screen.getByText('Approve');
      fireEvent.click(approveBtn);
      fireEvent.click(approveBtn);

      expect(api.decideTaskApproval).toHaveBeenCalledTimes(1);
    });
  });

  describe('Replay Control', () => {
    it('renders replay controls for terminal tasks', () => {
      const completedTask = { ...mockTask, status: TaskStatus.COMPLETED };
      const { container } = render(<TaskCard task={completedTask} />);
      expect(container.querySelector('.btn-replay')).toBeInTheDocument();
      expect(container.querySelector('.btn-replay-overrides')).toBeInTheDocument();

      const failedTask = { ...mockTask, status: TaskStatus.FAILED };
      const { container: containerFailed } = render(<TaskCard task={failedTask} />);
      expect(containerFailed.querySelector('.btn-replay')).toBeInTheDocument();
      expect(containerFailed.querySelector('.btn-replay-overrides')).toBeInTheDocument();

      const cancelledTask = { ...mockTask, status: TaskStatus.CANCELLED };
      const { container: containerCancelled } = render(<TaskCard task={cancelledTask} />);
      expect(containerCancelled.querySelector('.btn-replay')).toBeInTheDocument();
      expect(containerCancelled.querySelector('.btn-replay-overrides')).toBeInTheDocument();
    });

    it('does not render replay controls for non-terminal tasks', () => {
      const runningTask = { ...mockTask, status: TaskStatus.IN_PROGRESS };
      const { container } = render(<TaskCard task={runningTask} />);
      expect(container.querySelector('.btn-replay')).toBeNull();
      expect(container.querySelector('.btn-replay-overrides')).toBeNull();

      const pendingTask = { ...mockTask, status: TaskStatus.PENDING };
      const { container: containerPending } = render(<TaskCard task={pendingTask} />);
      expect(containerPending.querySelector('.btn-replay')).toBeNull();
      expect(containerPending.querySelector('.btn-replay-overrides')).toBeNull();
    });

    it('handles replay click and prevents propagation', async () => {
      const onRefresh = vi.fn();
      const onCardClick = vi.fn();
      const completedTask = { ...mockTask, status: TaskStatus.COMPLETED };
      vi.mocked(api.replayTask).mockResolvedValueOnce({} as TaskSnapshot);

      const { container } = render(
        <TaskCard task={completedTask} onRefresh={onRefresh} onClick={onCardClick} />
      );

      const replayBtn = container.querySelector('.btn-replay');
      fireEvent.click(replayBtn!);

      expect(api.replayTask).toHaveBeenCalledWith(completedTask.task_id);
      expect(onCardClick).not.toHaveBeenCalled();
      await vi.waitFor(() => expect(onRefresh).toHaveBeenCalled());
    });

    it('disables replay button during processing', async () => {
      const completedTask = { ...mockTask, status: TaskStatus.COMPLETED };
      let resolveReplay: (value: TaskSnapshot) => void;
      const replayPromise = new Promise<TaskSnapshot>((resolve) => {
        resolveReplay = resolve;
      });
      vi.mocked(api.replayTask).mockReturnValueOnce(replayPromise);

      const { container } = render(<TaskCard task={completedTask} />);
      const replayBtn = container.querySelector('.btn-replay') as HTMLButtonElement;

      fireEvent.click(replayBtn);
      expect(replayBtn.disabled).toBe(true);

      // @ts-expect-error: resolveReplay is captured
      resolveReplay({});
      await vi.waitFor(() => expect(replayBtn.disabled).toBe(false));
    });

    it('displays error message when replay fails', async () => {
      const completedTask = { ...mockTask, status: TaskStatus.COMPLETED };
      vi.mocked(api.replayTask).mockRejectedValueOnce(new Error('Replay failed server-side'));

      const { container } = render(<TaskCard task={completedTask} />);
      const replayBtn = container.querySelector('.btn-replay');

      fireEvent.click(replayBtn!);

      await vi.waitFor(() => {
        expect(screen.getByText('Replay failed server-side')).toBeInTheDocument();
      });
    });

    it('handles non-Error objects in catch blocks', async () => {
      vi.mocked(api.replayTask).mockRejectedValueOnce('String error');
      const completedTask = { ...mockTask, status: TaskStatus.COMPLETED };
      render(<TaskCard task={completedTask} />);

      fireEvent.click(screen.getByTitle('Replay task (unchanged)'));

      await vi.waitFor(() => {
        expect(screen.getByText('Failed to replay task')).toBeInTheDocument();
      });
    });

    it('handles empty run status string', () => {
      const task = { ...mockTask, latest_run_status: '' };
      const { container } = render(<TaskCard task={task} />);
      expect(container.querySelector('.run-status')).toBeNull();
    });

    it('opens replay-with-overrides modal and prevents propagation', () => {
      const onCardClick = vi.fn();
      const completedTask = { ...mockTask, status: TaskStatus.COMPLETED };
      render(<TaskCard task={completedTask} onClick={onCardClick} />);

      fireEvent.click(screen.getByTitle('Replay task with overrides'));

      expect(screen.getByRole('dialog', { name: 'Replay With Overrides' })).toBeInTheDocument();
      expect(onCardClick).not.toHaveBeenCalled();
    });

    it('submits replay-with-overrides payload and refreshes', async () => {
      const onRefresh = vi.fn();
      const completedTask = { ...mockTask, status: TaskStatus.COMPLETED };
      vi.mocked(api.replayTask).mockResolvedValueOnce({} as TaskSnapshot);

      render(<TaskCard task={completedTask} onRefresh={onRefresh} />);

      fireEvent.click(screen.getByTitle('Replay task with overrides'));
      fireEvent.change(screen.getByLabelText('Worker Override'), { target: { value: 'openrouter' } });
      fireEvent.change(screen.getByLabelText('Constraints Override (JSON object)'), {
        target: { value: '{"execution_mode":"apply"}' },
      });
      const dialog = screen.getByRole('dialog', { name: 'Replay With Overrides' });
      fireEvent.click(within(dialog).getByRole('button', { name: 'Replay Task' }));

      await vi.waitFor(() =>
        expect(api.replayTask).toHaveBeenCalledWith(completedTask.task_id, {
          worker_override: 'openrouter',
          constraints: { execution_mode: 'apply' },
        })
      );
      expect(onRefresh).toHaveBeenCalled();
    });
  });
});
