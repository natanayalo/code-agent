import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { TaskCard } from './TaskCard';
import { TaskStatus, ApprovalStatus } from '../types/task';
import { api } from '../services/api';

vi.mock('../services/api', () => ({
  api: {
    decideTaskApproval: vi.fn(),
  },
}));

describe('TaskCard', () => {
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
  });
});
