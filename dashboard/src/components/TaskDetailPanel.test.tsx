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

function buildLatestRun(overrides: Partial<NonNullable<TaskSnapshot['latest_run']>> = {}) {
  return {
    run_id: 'run-base',
    session_id: 'session-1',
    worker_type: 'codex',
    workspace_id: 'workspace-1',
    status: 'success',
    started_at: '2026-04-28T00:01:05.000Z',
    finished_at: '2026-04-28T00:01:10.000Z',
    summary: 'Done',
    requested_permission: null,
    budget_usage: {},
    verifier_outcome: {},
    commands_run: [],
    files_changed_count: 0,
    artifact_index: [],
    artifacts: [],
    ...overrides,
  };
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

  it('renders timeline, command logs, and artifact metadata when run details exist', () => {
    const task = buildTask({
      timeline: [
        {
          event_type: 'worker_selected',
          attempt_number: 0,
          sequence_number: 1,
          message: 'Selected codex for execution',
          payload: { worker: 'codex' },
          created_at: '2026-04-28T00:01:00.000Z',
        },
      ],
      latest_run: {
        run_id: 'run-1',
        session_id: 'session-1',
        worker_type: 'codex',
        workspace_id: 'workspace-1',
        status: 'success',
        started_at: '2026-04-28T00:01:05.000Z',
        finished_at: '2026-04-28T00:01:10.000Z',
        summary: 'Done',
        requested_permission: null,
        budget_usage: { iterations_used: 1 },
        verifier_outcome: { status: 'pass' },
        commands_run: [
          {
            command: "printf 'done\\n' > note.txt",
            exit_code: 0,
            duration_seconds: 0.1,
            stdout_artifact_uri: 'artifacts/stdout.log',
            stderr_artifact_uri: 'artifacts/stderr.log',
          },
        ],
        files_changed_count: 1,
        artifact_index: [
          {
            name: 'workspace',
            uri: '/tmp/workspace-task-1',
            artifact_type: 'workspace',
            artifact_metadata: { retained: true },
          },
        ],
        artifacts: [],
      },
    });

    render(<TaskDetailPanel task={task} loading={false} error={null} onClose={vi.fn()} />);

    expect(screen.getByText('Timeline')).toBeInTheDocument();
    expect(screen.getByText('Selected codex for execution')).toBeInTheDocument();
    expect(screen.getByText(/worker selected/i)).toBeInTheDocument();
    expect(screen.getByText('Commands & Logs')).toBeInTheDocument();
    expect(screen.getByText("printf 'done\\n' > note.txt")).toBeInTheDocument();
    expect(screen.getByText(/artifacts\/stdout\.log/i)).toBeInTheDocument();
    expect(screen.getByText(/artifacts\/stderr\.log/i)).toBeInTheDocument();
    expect(screen.getByText('Artifacts')).toBeInTheDocument();
    expect(screen.getByText('/tmp/workspace-task-1')).toBeInTheDocument();
    expect(screen.getByText(/"retained": true/)).toBeInTheDocument();
  });

  it('renders no-data states for timeline, commands, and artifacts', () => {
    render(<TaskDetailPanel task={baseTask} loading={false} error={null} onClose={vi.fn()} />);

    expect(screen.getByText('No timeline events recorded yet.')).toBeInTheDocument();
    expect(screen.getByText('No run metadata available yet.')).toBeInTheDocument();
    expect(screen.getByText('No artifacts persisted for the latest run.')).toBeInTheDocument();
  });

  it('renders loading and fallback error states without task data', () => {
    const { rerender } = render(
      <TaskDetailPanel task={null} loading={true} error={null} onClose={vi.fn()} />
    );
    expect(screen.getByText('Loading task detail...')).toBeInTheDocument();

    rerender(<TaskDetailPanel task={null} loading={false} error={'oops'} onClose={vi.fn()} />);
    expect(screen.getByText('Failed to load task detail.')).toBeInTheDocument();
  });

  it('falls back to persisted artifact rows and handles edge-case timeline/log branches', () => {
    const circularPayload: { self?: unknown } = {};
    circularPayload.self = circularPayload;

    const task = buildTask({
      task_spec: null,
      timeline: [
        {
          event_type: 'worker_selected',
          attempt_number: 2,
          sequence_number: 7,
          message: null,
          payload: circularPayload as unknown as Record<string, unknown>,
          created_at: 'not-a-date',
        },
      ],
      latest_run: buildLatestRun({
        commands_run: [
          {
            command: 'sleep 2',
            exit_code: 124,
            timed_out: true,
            duration_seconds: 2,
          },
          {
            command: 'echo no-duration',
            exit_code: 0,
          },
          {
            command: 'echo bad-duration',
            exit_code: 0,
            duration_seconds: Number.NaN,
          },
        ],
        artifact_index: [],
        artifacts: [
          {
            artifact_id: 'artifact-1',
            artifact_type: 'log',
            name: 'stderr.log',
            uri: 'artifacts/stderr.log',
            artifact_metadata: null,
          },
        ],
      }),
    });

    render(<TaskDetailPanel task={task} loading={false} error={null} onClose={vi.fn()} />);

    expect(screen.getByText('No TaskSpec captured for this task.')).toBeInTheDocument();
    expect(screen.getByText(/attempt 2/i)).toBeInTheDocument();
    expect(screen.getByText(/not-a-date/i)).toBeInTheDocument();
    expect(screen.getByText('[object Object]')).toBeInTheDocument();
    expect(screen.getByText(/timed out/i)).toBeInTheDocument();
    expect(screen.getByText(/2\.0s/i)).toBeInTheDocument();
    expect(screen.getByText('artifacts/stderr.log')).toBeInTheDocument();
  });

  it('renders latest-run empty command/artifact states when arrays are present but empty', () => {
    const task = buildTask({
      latest_run: buildLatestRun(),
    });

    render(<TaskDetailPanel task={task} loading={false} error={null} onClose={vi.fn()} />);

    expect(screen.getByText('No commands captured for the latest run.')).toBeInTheDocument();
    expect(screen.getByText('No artifacts persisted for the latest run.')).toBeInTheDocument();
  });
});
