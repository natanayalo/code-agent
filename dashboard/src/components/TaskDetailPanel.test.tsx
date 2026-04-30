import React from 'react';
import { render, screen, within } from '@testing-library/react';
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

  it('exposes an accessible label for the close button', () => {
    render(<TaskDetailPanel task={baseTask} loading={false} error={null} onClose={vi.fn()} />);

    expect(screen.getByRole('button', { name: 'Close task detail' })).toBeInTheDocument();
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
    expect(screen.getByText('No trace metadata available yet.')).toBeInTheDocument();
    expect(screen.getByText('No artifacts persisted for the latest run.')).toBeInTheDocument();
  });

  it('renders trace observability details from run and timeline metadata', () => {
    const task = buildTask({
      timeline: [
        {
          event_type: 'worker_completed',
          attempt_number: 0,
          sequence_number: 10,
          message: 'Worker completed',
          payload: {
            telemetry: {
              spans: [{ status: 'ok' }, { status: 'error' }, { status: 'ok' }],
              trace_ids: ['trace-from-array'],
            },
          },
          created_at: '2026-04-28T00:10:00.000Z',
        },
      ],
      latest_run: buildLatestRun({
        budget_usage: {
          telemetry: {
            trace_id: 'trace-123',
            trace_url: 'https://smith.langchain.com/public/trace/trace-123',
            span_status_counts: {
              ok: 4,
              warning: 1,
            },
          },
        },
        verifier_outcome: {
          traceparent: '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01',
        },
      }),
    });

    render(<TaskDetailPanel task={task} loading={false} error={null} onClose={vi.fn()} />);

    expect(screen.getByText('Trace Observability')).toBeInTheDocument();
    expect(screen.getByText('Trace IDs')).toBeInTheDocument();
    expect(screen.getByText('trace-123')).toBeInTheDocument();
    expect(screen.getByText('4bf92f3577b34da6a3ce929d0e0e4736')).toBeInTheDocument();
    expect(screen.getByText('trace-from-array')).toBeInTheDocument();
    expect(screen.getByText('Provider Deep Links')).toBeInTheDocument();
    expect(
      screen.getByRole('link', {
        name: 'https://smith.langchain.com/public/trace/trace-123',
      })
    ).toHaveAttribute('href', 'https://smith.langchain.com/public/trace/trace-123');
    expect(
      screen.getByRole('link', {
        name: 'https://smith.langchain.com/public/trace/trace-123',
      })
    ).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByText('Span Status Summary')).toBeInTheDocument();
    const okRow = screen.getByText('Ok:').closest('li');
    const warningRow = screen.getByText('Warning:').closest('li');
    const errorRow = screen.getByText('Error:').closest('li');
    expect(okRow).not.toBeNull();
    expect(warningRow).not.toBeNull();
    expect(errorRow).not.toBeNull();
    expect(within(okRow as HTMLElement).getByText('6')).toBeInTheDocument();
    expect(within(warningRow as HTMLElement).getByText('1')).toBeInTheDocument();
    expect(within(errorRow as HTMLElement).getByText('1')).toBeInTheDocument();
  });

  it('does not map deceptive hostnames to trusted trace providers', () => {
    const task = buildTask({
      latest_run: buildLatestRun({
        budget_usage: {
          telemetry: {
            trace_url: 'https://smith.langchain.com.evil.example/public/trace/abc',
          },
        },
      }),
    });

    render(<TaskDetailPanel task={task} loading={false} error={null} onClose={vi.fn()} />);

    expect(screen.getByText('Provider Deep Links')).toBeInTheDocument();
    expect(screen.queryByText('LangSmith')).not.toBeInTheDocument();
    expect(screen.getByText('smith.langchain.com.evil.example')).toBeInTheDocument();
  });

  it('renders loading and fallback error states without task data', () => {
    const { rerender } = render(
      <TaskDetailPanel task={null} loading={true} error={null} onClose={vi.fn()} />
    );
    expect(screen.getByText('Loading task detail...')).toBeInTheDocument();

    rerender(<TaskDetailPanel task={null} loading={false} error={'oops'} onClose={vi.fn()} />);
    expect(screen.getByText('Failed to load task detail.')).toBeInTheDocument();
  });

  it('renders Error instance messages when detail loading fails', () => {
    render(
      <TaskDetailPanel task={null} loading={false} error={new Error('Boom')} onClose={vi.fn()} />
    );
    expect(screen.getByText('Boom')).toBeInTheDocument();
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
    expect(screen.getByText(/Unserializable Object value/i)).toBeInTheDocument();
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

  it('covers sparse payload fallbacks and event-type tie-break ordering', () => {
    const task = buildTask({
      task_spec: {
        ...baseTask.task_spec!,
        acceptance_criteria: [],
        clarification_questions: [],
      },
      timeline: [
        {
          event_type: 'z_event',
          attempt_number: 0,
          sequence_number: 1,
          message: 'Second alphabetical event',
          payload: null,
          created_at: '',
        },
        {
          event_type: 'a_event',
          attempt_number: 0,
          sequence_number: 1,
          message: 'First alphabetical event',
          payload: null,
          created_at: '',
        },
      ],
      latest_run: buildLatestRun({
        commands_run: [
          {
            exit_code: 0,
          },
        ],
        artifact_index: [
          {},
        ],
      }),
    });

    render(<TaskDetailPanel task={task} loading={false} error={null} onClose={vi.fn()} />);

    expect(screen.queryByText('Acceptance Criteria')).not.toBeInTheDocument();
    expect(screen.queryByText('Clarification Questions')).not.toBeInTheDocument();
    expect(screen.getAllByText(/unknown time/i).length).toBeGreaterThan(0);
    expect(screen.getByText('Command 1')).toBeInTheDocument();
    expect(screen.getByText('artifact')).toBeInTheDocument();
    expect(screen.getByText('No URI')).toBeInTheDocument();

    const first = screen.getByText('First alphabetical event');
    const second = screen.getByText('Second alphabetical event');
    expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('renders timeline in stable order by sequence then created_at', () => {
    const task = buildTask({
      timeline: [
        {
          event_type: 'worker_selected',
          attempt_number: 0,
          sequence_number: 2,
          message: 'Third event',
          payload: null,
          created_at: '2026-04-28T00:03:00.000Z',
        },
        {
          event_type: 'task_ingested',
          attempt_number: 0,
          sequence_number: 1,
          message: 'Second event',
          payload: null,
          created_at: '2026-04-28T00:02:00.000Z',
        },
        {
          event_type: 'task_ingested',
          attempt_number: 0,
          sequence_number: 1,
          message: 'First event',
          payload: null,
          created_at: '2026-04-28T00:01:00.000Z',
        },
      ],
    });

    render(<TaskDetailPanel task={task} loading={false} error={null} onClose={vi.fn()} />);

    const first = screen.getByText('First event');
    const second = screen.getByText('Second event');
    const third = screen.getByText('Third event');

    expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(second.compareDocumentPosition(third) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
