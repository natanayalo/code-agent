import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { OperatorInbox } from './OperatorInbox';
import { TaskStatus, TaskSummarySnapshot } from '../types/task';
import { api } from '../services/api';

vi.mock('../services/api', () => ({
  api: {
    replayTask: vi.fn(),
  },
}));

describe('OperatorInbox', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders truncation markup and keeps pending count visible', () => {
    const onOpenTask = vi.fn();
    const longTaskText =
      'This is a very long task text that should truncate in the inbox row without hiding count';

    render(
      <OperatorInbox
        interactions={[
          {
            interaction: {
              interaction_id: 'int-1',
              interaction_type: 'clarification',
              status: 'pending',
              summary: 'Need more details',
              hitl_mode: 'require_approval',
              data: {},
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            },
            task_id: 'task-1',
            task_text: longTaskText,
            status: 'in_progress',
            priority: 1,
          },
        ]}
        selectedTaskId={null}
        onOpenTask={onOpenTask}
      />
    );

    const text = screen.getByTitle(longTaskText);
    expect(text).toHaveClass('operator-inbox-text', 'truncate');
    expect(screen.getByText('clarification')).toHaveClass('operator-inbox-type', 'badge');

    fireEvent.click(screen.getByRole('button', { name: /very long task text/i }));
    expect(onOpenTask).toHaveBeenCalledWith('task-1');
  });

  it('renders synchronized PR and CI delivery items and prioritizes actionable items', () => {
    const onOpenTask = vi.fn();
    const tasks: TaskSummarySnapshot[] = [
      {
        task_id: 'task-passed',
        session_id: 's-1',
        status: TaskStatus.COMPLETED,
        task_text: 'Fix typo in documentation',
        priority: 1,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        delivery_metadata: {
          pr_number: 101,
          pr_url: 'https://github.com/octocat/code-agent/pull/101',
          pr_state: 'open',
          pr_draft: false,
          ci_status: 'passed',
          source_issue_number: 40,
          source_issue_url: 'https://github.com/octocat/code-agent/issues/40',
        },
      },
      {
        task_id: 'task-failed',
        session_id: 's-2',
        status: TaskStatus.FAILED,
        task_text: 'Fix memory leak in parser',
        priority: 2,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        delivery_metadata: {
          pr_number: 105,
          pr_url: 'https://github.com/octocat/code-agent/pull/105',
          pr_state: 'open',
          pr_draft: true,
          ci_status: 'failed',
          ci_failed_jobs: ['unit-tests', 'lint'],
          source_issue_number: 42,
          source_issue_url: 'https://github.com/octocat/code-agent/issues/42',
          check_runs: [
            {
              id: 9001,
              name: 'unit-tests',
              status: 'completed',
              conclusion: 'failure',
              html_url: 'https://github.com/octocat/code-agent/runs/9001',
            },
          ],
        },
      },
      {
        task_id: 'task-pending',
        session_id: 's-3',
        status: TaskStatus.IN_PROGRESS,
        task_text: 'Add dashboard telemetry',
        priority: 3,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        delivery_metadata: {
          pr_number: 108,
          pr_url: 'https://github.com/octocat/code-agent/pull/108',
          pr_state: 'open',
          pr_draft: true,
          ci_status: 'pending',
          source_issue_number: 45,
          source_issue_url: 'https://github.com/octocat/code-agent/issues/45',
        },
      },
    ];

    render(
      <OperatorInbox
        interactions={[]}
        selectedTaskId={null}
        onOpenTask={onOpenTask}
        tasks={tasks}
      />
    );

    // Header counter reflects items
    expect(screen.getByText(/3 PR\/CI items/i)).toBeInTheDocument();

    // Verify actionable sorting: failed CI item should appear first
    const deliveryCards = screen.getAllByRole('listitem');
    expect(deliveryCards[0]).toHaveTextContent('Fix memory leak in parser');
    expect(deliveryCards[1]).toHaveTextContent('Add dashboard telemetry');
    expect(deliveryCards[2]).toHaveTextContent('Fix typo in documentation');

    // Badges & safe next action assertions
    expect(screen.getByText('CI failed (unit-tests, lint) — inspect check logs or replay task')).toBeInTheDocument();
    expect(screen.getByText('CI checks in progress — monitor draft PR checks')).toBeInTheDocument();
    expect(screen.getByText('CI passed — ready for operator review on GitHub')).toBeInTheDocument();

    // Direct links
    const issueLink = screen.getByRole('link', { name: /Issue #42/i });
    expect(issueLink).toHaveAttribute('href', 'https://github.com/octocat/code-agent/issues/42');

    const prLink = screen.getByRole('link', { name: /PR #105/i });
    expect(prLink).toHaveAttribute('href', 'https://github.com/octocat/code-agent/pull/105');

    const checkLink = screen.getByRole('link', { name: /View Check \(unit-tests\)/i });
    expect(checkLink).toHaveAttribute('href', 'https://github.com/octocat/code-agent/runs/9001');

    // Inspect task button
    const inspectButtons = screen.getAllByRole('button', { name: /Inspect Task/i });
    fireEvent.click(inspectButtons[0]);
    expect(onOpenTask).toHaveBeenCalledWith('task-failed');
  });

  it('triggers authenticated replay from delivery item when CI failed', async () => {
    const onRefresh = vi.fn();
    (api.replayTask as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      task_id: 'task-failed',
      status: TaskStatus.PENDING,
    });

    const tasks: TaskSummarySnapshot[] = [
      {
        task_id: 'task-failed',
        session_id: 's-1',
        status: TaskStatus.FAILED,
        task_text: 'Broken PR task',
        priority: 1,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        delivery_metadata: {
          pr_number: 200,
          pr_url: 'https://github.com/octocat/code-agent/pull/200',
          ci_status: 'failed',
          ci_failed_jobs: ['build'],
        },
      },
    ];

    render(
      <OperatorInbox
        interactions={[]}
        selectedTaskId={null}
        onOpenTask={vi.fn()}
        tasks={tasks}
        onRefresh={onRefresh}
      />
    );

    const replayBtn = screen.getByRole('button', { name: /Replay Task/i });
    fireEvent.click(replayBtn);

    expect(api.replayTask).toHaveBeenCalledWith('task-failed');
    await waitFor(() => {
      expect(onRefresh).toHaveBeenCalled();
    });
  });

  it('displays replay error when replayTask rejects', async () => {
    (api.replayTask as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error('Replay rejected: worker busy')
    );

    const tasks: TaskSummarySnapshot[] = [
      {
        task_id: 'task-failed',
        session_id: 's-1',
        status: TaskStatus.FAILED,
        task_text: 'Failing replay task',
        priority: 1,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        delivery_metadata: {
          pr_number: 300,
          ci_status: 'failed',
        },
      },
    ];

    render(
      <OperatorInbox
        interactions={[]}
        selectedTaskId={null}
        onOpenTask={vi.fn()}
        tasks={tasks}
      />
    );

    const replayBtn = screen.getByRole('button', { name: /Replay Task/i });
    fireEvent.click(replayBtn);

    await waitFor(() => {
      expect(screen.getByText('Replay rejected: worker busy')).toBeInTheDocument();
    });
  });

  it('renders singular counts and covers merged, draft passed, and unknown CI states', () => {
    const tasks: TaskSummarySnapshot[] = [
      {
        task_id: 'task-draft-passed',
        session_id: 's-1',
        status: TaskStatus.COMPLETED,
        task_text: 'Draft passed task',
        priority: 1,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        delivery_metadata: {
          pr_number: 401,
          pr_draft: true,
          ci_status: 'passed',
        },
      },
      {
        task_id: 'task-merged',
        session_id: 's-2',
        status: TaskStatus.COMPLETED,
        task_text: 'Merged task',
        priority: 2,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        delivery_metadata: {
          pr_number: 402,
          pr_state: 'merged',
        },
      },
      {
        task_id: 'task-unknown-ci',
        session_id: 's-3',
        status: TaskStatus.IN_PROGRESS,
        task_text: 'Unknown CI task',
        priority: 3,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        delivery_metadata: {
          pr_number: 403,
          ci_status: 'custom_state',
        },
      },
      {
        task_id: 'task-no-delivery',
        session_id: 's-4',
        status: TaskStatus.PENDING,
        task_text: 'Task without delivery',
        priority: 4,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ];

    render(
      <OperatorInbox
        interactions={[
          {
            interaction: {
              interaction_id: 'int-single',
              interaction_type: 'permission',
              status: 'pending',
              summary: 'Approval required',
              hitl_mode: 'require_approval',
              data: {},
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            },
            task_id: 'task-int-1',
            task_text: 'Single interaction task',
            status: 'in_progress',
            priority: 1,
          },
        ]}
        selectedTaskId={null}
        onOpenTask={vi.fn()}
        tasks={tasks}
      />
    );

    expect(screen.getByText(/1 pending interaction · 3 PR\/CI items/i)).toBeInTheDocument();
    expect(screen.getByText('CI passed — review draft PR on GitHub when ready')).toBeInTheDocument();
    expect(screen.getByText('Pull request merged')).toBeInTheDocument();
    expect(screen.getByText('Review pull request or check status on GitHub')).toBeInTheDocument();
    expect(screen.getByText('CI: custom_state')).toHaveClass('ci-badge', 'ci-unknown');
    expect(screen.queryByText('Task without delivery')).not.toBeInTheDocument();
  });
});
