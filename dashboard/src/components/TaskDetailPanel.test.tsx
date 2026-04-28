import React from 'react';
import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TaskDetailPanel } from './TaskDetailPanel';
import { TaskSnapshot, TaskStatus } from '../types/task';

const baseTask: TaskSnapshot = {
  task_id: 'task-1',
  session_id: 'session-1',
  status: TaskStatus.IN_PROGRESS,
  task_text: 'Review task detail behavior',
  priority: 0,
  created_at: '2026-04-28T00:00:00.000Z',
  updated_at: '2026-04-28T00:00:00.000Z',
  timeline: [],
  task_spec: {
    goal: 'Show task detail data',
    assumptions: [],
    acceptance_criteria: ['Criterion A'],
    non_goals: [],
    risk_level: 'low',
    task_type: 'feature',
    allowed_actions: [],
    forbidden_actions: [],
    verification_commands: [],
    expected_artifacts: [],
    requires_clarification: false,
    clarification_questions: ['Question A'],
    requires_permission: false,
    delivery_mode: 'summary',
  },
  pending_interactions: [],
};

function buildTask(overrides: Partial<TaskSnapshot> = {}): TaskSnapshot {
  return { ...baseTask, ...overrides };
}

describe('TaskDetailPanel', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('does not emit duplicate-key warnings for repeated list items', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const task = buildTask({
      task_spec: {
        ...baseTask.task_spec!,
        acceptance_criteria: ['Repeat me', 'Repeat me'],
        clarification_questions: ['Same question?', 'Same question?'],
      },
    });

    render(
      <TaskDetailPanel task={task} loading={false} error={null} onClose={vi.fn()} />
    );

    const duplicateKeyWarnings = consoleError.mock.calls.filter(([message]) =>
      typeof message === 'string' && message.includes('unique "key"')
    );
    expect(duplicateKeyWarnings).toHaveLength(0);
    expect(screen.getAllByText('Repeat me')).toHaveLength(2);
  });

  it('handles nullish labels without crashing when payload fields are missing', () => {
    const malformedTask = {
      ...baseTask,
      status: undefined,
      task_spec: {
        ...baseTask.task_spec!,
        risk_level: undefined,
        task_type: null,
        delivery_mode: undefined,
      },
      pending_interactions: [
        {
          interaction_id: 'interaction-1',
          interaction_type: undefined,
          status: null,
          summary: 'Need operator input',
          data: {},
          response_data: null,
          created_at: '2026-04-28T00:00:00.000Z',
          updated_at: '2026-04-28T00:00:00.000Z',
        },
      ],
    } as unknown as TaskSnapshot;

    expect(() =>
      render(
        <TaskDetailPanel task={malformedTask} loading={false} error={null} onClose={vi.fn()} />
      )
    ).not.toThrow();
    expect(screen.getByText('Need operator input')).toBeInTheDocument();
  });
});
