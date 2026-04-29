import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { TaskBoard } from './TaskBoard';
import { TaskStatus } from '../types/task';

describe('TaskBoard', () => {
  const mockTasks = [
    {
      task_id: '1',
      task_text: 'Task 1',
      status: TaskStatus.PENDING,
      created_at: '2024-01-01T10:00:00Z',
    },
    {
      task_id: '2',
      task_text: 'Task 2',
      status: TaskStatus.COMPLETED,
      created_at: '2024-01-01T11:00:00Z',
    },
    {
      task_id: '3',
      task_text: 'Task 3',
      status: TaskStatus.FAILED,
      created_at: '2024-01-01T09:00:00Z',
    },
  ];

  const mockRefetch = vi.fn();

  it('renders tasks in correct columns', () => {
    render(
      <TaskBoard
        tasks={mockTasks}
        loading={false}
        isFetching={false}
        error={null}
        refetch={mockRefetch}
      />
    );

    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getByText('Failed')).toBeInTheDocument();

    expect(screen.getByText('Task 1')).toBeInTheDocument();
    expect(screen.getByText('Task 2')).toBeInTheDocument();
    expect(screen.getByText('Task 3')).toBeInTheDocument();
  });

  it('toggles view mode', () => {
    render(
      <TaskBoard
        tasks={mockTasks}
        loading={false}
        isFetching={false}
        error={null}
        refetch={mockRefetch}
      />
    );

    const listToggle = screen.getByLabelText('List view');
    const gridToggle = screen.getByLabelText('Grid view');

    expect(gridToggle).toHaveClass('active');

    fireEvent.click(listToggle);
    expect(listToggle).toHaveClass('active');
    expect(gridToggle).not.toHaveClass('active');

    fireEvent.click(gridToggle);
    expect(gridToggle).toHaveClass('active');
  });

  it('handles loading state', () => {
    render(
      <TaskBoard
        tasks={[]}
        loading={true}
        isFetching={true}
        error={null}
        refetch={mockRefetch}
      />
    );

    expect(screen.getAllByText('Loading tasks...')).toHaveLength(3);
  });

  it('handles error state', () => {
    const error = new Error('Network Error');
    render(
      <TaskBoard
        tasks={[]}
        loading={false}
        isFetching={false}
        error={error}
        refetch={mockRefetch}
      />
    );

    expect(screen.getByText('Network Error')).toBeInTheDocument();
    const retryButton = screen.getByText('Try Again');
    fireEvent.click(retryButton);
    expect(mockRefetch).toHaveBeenCalled();
  });

  it('handles refresh button', () => {
    render(
      <TaskBoard
        tasks={mockTasks}
        loading={false}
        isFetching={false}
        error={null}
        refetch={mockRefetch}
      />
    );

    const refreshButton = screen.getByLabelText('Refresh tasks');
    fireEvent.click(refreshButton);
    expect(mockRefetch).toHaveBeenCalled();
  });

  it('sorts tasks by date in columns', () => {
    const sameStatusTasks = [
      {
        task_id: 'old',
        task_text: 'Old Task',
        status: TaskStatus.COMPLETED,
        created_at: '2024-01-01T10:00:00Z',
      },
      {
        task_id: 'new',
        task_text: 'New Task',
        status: TaskStatus.COMPLETED,
        created_at: '2024-01-01T11:00:00Z',
      },
    ];

    render(
      <TaskBoard
        tasks={sameStatusTasks}
        loading={false}
        isFetching={false}
        error={null}
        refetch={mockRefetch}
      />
    );

    const completedColumn = screen.getByText('Completed').closest('.board-column')!;
    const taskElements = within(completedColumn as HTMLElement).getAllByRole('heading', { level: 3 });
    const taskTexts = taskElements.map(el => el.textContent).slice(1);

    expect(taskTexts[0]).toBe('New Task');
    expect(taskTexts[1]).toBe('Old Task');
  });

  it('handles missing created_at in sorting', () => {
    const tasksWithMissingDate = [
      {
        task_id: 'no-date',
        task_text: 'No Date Task',
        status: TaskStatus.COMPLETED,
        created_at: null,
      },
      {
        task_id: 'with-date',
        task_text: 'With Date Task',
        status: TaskStatus.COMPLETED,
        created_at: '2024-01-01T10:00:00Z',
      },
    ];

    render(
      <TaskBoard
        // @ts-expect-error: testing missing created_at
        tasks={tasksWithMissingDate}
        loading={false}
        isFetching={false}
        error={null}
        refetch={mockRefetch}
      />
    );

    const completedColumn = screen.getByText('Completed').closest('.board-column')!;
    const taskElements = within(completedColumn as HTMLElement).getAllByRole('heading', { level: 3 });
    const taskTexts = taskElements.map(el => el.textContent).slice(1);

    expect(taskTexts[0]).toBe('With Date Task');
    expect(taskTexts[1]).toBe('No Date Task');
  });

  it('handles unknown status gracefully', () => {
    const unknownTask = {
      task_id: 'unknown',
      task_text: 'Unknown Status Task',
      // @ts-expect-error: testing unknown status
      status: 'invalid_status',
      created_at: new Date().toISOString(),
    };

    render(
      <TaskBoard
        tasks={[unknownTask]}
        loading={false}
        isFetching={false}
        error={null}
        refetch={mockRefetch}
      />
    );

    expect(screen.queryByText('Unknown Status Task')).toBeNull();
  });

  it('forces tasks awaiting approval into the Active column', () => {
    const approvalTask = {
      task_id: 'approval-1',
      task_text: 'Awaiting Approval',
      status: TaskStatus.COMPLETED, // Even if marked completed, if approval is pending it stays active
      approval_status: 'pending',
      created_at: new Date().toISOString(),
    };

    render(
      <TaskBoard
        // @ts-expect-error: testing pending approval logic
        tasks={[approvalTask]}
        loading={false}
        isFetching={false}
        error={null}
        refetch={mockRefetch}
      />
    );

    const activeColumn = screen.getByText('Active').closest('.board-column')!;
    expect(within(activeColumn as HTMLElement).getByText('Awaiting Approval')).toBeInTheDocument();
  });

  it('uses one shared replay-overrides modal instance', () => {
    render(
      <TaskBoard
        tasks={mockTasks}
        loading={false}
        isFetching={false}
        error={null}
        refetch={mockRefetch}
      />
    );

    const replayOverrideButtons = screen.getAllByTitle('Replay task with overrides');
    fireEvent.click(replayOverrideButtons[0]);
    expect(screen.getAllByRole('dialog', { name: 'Replay With Overrides' })).toHaveLength(1);

    fireEvent.click(replayOverrideButtons[1]);
    expect(screen.getAllByRole('dialog', { name: 'Replay With Overrides' })).toHaveLength(1);
  });
});
