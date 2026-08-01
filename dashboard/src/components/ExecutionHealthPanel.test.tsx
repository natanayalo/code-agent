import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ExecutionHealthPanel } from './ExecutionHealthPanel';
import { ExecutionHealthMetrics, ReadinessSnapshot } from '../types/metrics';

const healthyReadiness: ReadinessSnapshot = {
  status: 'ready',
  checked_at: '2026-08-01T12:00:00Z',
  components: Object.fromEntries(
    ['postgres', 'temporal', 'worker', 'dispatcher'].map((name) => [
      name,
      {
        status: 'ready',
        reasons: [],
        last_observed_at: name === 'worker' ? '2026-08-01T11:59:58Z' : null,
      },
    ]),
  ),
  degraded_reasons: [],
};

const healthyExecutionHealth: ExecutionHealthMetrics = {
  outbox: {
    pending_count: 0,
    retrying_count: 0,
    dead_letter_count: 0,
    oldest_unresolved_age_seconds: null,
    oldest_eligible_age_seconds: null,
    affected_task_ids: [],
    affected_task_ids_truncated: false,
  },
  workers: {
    fresh_count: 1,
    stale_count: 0,
    fresh_dispatcher_count: 1,
    freshest_heartbeat_at: '2026-08-01T11:59:58Z',
    freshest_heartbeat_age_seconds: 2,
    freshest_dispatcher_heartbeat_at: '2026-08-01T11:59:57Z',
    freshest_dispatcher_heartbeat_age_seconds: 3,
  },
  interactions: {
    pending_count: 0,
    stuck_count: 0,
    oldest_pending_age_seconds: null,
    affected_task_ids: [],
    affected_task_ids_truncated: false,
  },
  reconciliation: {
    status: 'ok',
    divergence_count: 0,
    affected_task_ids: [],
    affected_task_ids_truncated: false,
    checked_at: '2026-08-01T12:00:00Z',
  },
  degraded_reasons: [],
};

const defaultProps = {
  readiness: healthyReadiness,
  executionHealth: healthyExecutionHealth,
  readinessLoading: false,
  executionHealthLoading: false,
  readinessError: null,
  executionHealthError: null,
  refreshing: false,
  onRefresh: vi.fn(),
};

describe('ExecutionHealthPanel', () => {
  it('renders ready dependencies, operational signals, and a clear recovery state', () => {
    render(<ExecutionHealthPanel {...defaultProps} />);

    expect(screen.getByRole('heading', { name: 'Execution Status' })).toBeInTheDocument();
    expect(screen.getByText('Execution ready')).toBeInTheDocument();
    const dependencies = screen.getByLabelText('Execution dependencies');
    for (const name of ['Postgres', 'Temporal', 'Worker', 'Dispatcher']) {
      expect(within(dependencies).getByRole('heading', { name })).toBeInTheDocument();
    }
    expect(screen.getByRole('heading', { name: 'Command Outbox' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Terminal Reconciliation' })).toBeInTheDocument();
    expect(screen.getByText('No degraded conditions reported.')).toBeInTheDocument();
  });

  it('renders every degraded reason, affected tasks, truncation, and unknown reconciliation', () => {
    const readiness: ReadinessSnapshot = {
      ...healthyReadiness,
      status: 'not_ready',
      components: {
        postgres: { status: 'not_ready', reasons: ['postgres_unavailable'], last_observed_at: null },
        temporal: { status: 'not_ready', reasons: ['temporal_unavailable'], last_observed_at: null },
        worker: { status: 'not_ready', reasons: ['worker_unavailable'], last_observed_at: null },
        dispatcher: {
          status: 'not_ready',
          reasons: ['dispatcher_unavailable', 'dispatcher_backlog_stale'],
          last_observed_at: 'not-a-date',
        },
      },
      degraded_reasons: ['task_service_unconfigured'],
    };
    const executionHealth: ExecutionHealthMetrics = {
      ...healthyExecutionHealth,
      outbox: {
        pending_count: 4,
        retrying_count: 2,
        dead_letter_count: 1,
        oldest_unresolved_age_seconds: 90,
        oldest_eligible_age_seconds: 7200,
        affected_task_ids: ['task-outbox'],
        affected_task_ids_truncated: true,
      },
      interactions: {
        pending_count: 2,
        stuck_count: 1,
        oldest_pending_age_seconds: 30,
        affected_task_ids: ['task-wait'],
        affected_task_ids_truncated: false,
      },
      reconciliation: {
        status: 'unknown',
        divergence_count: null,
        affected_task_ids: ['task-reconcile'],
        affected_task_ids_truncated: false,
        checked_at: 'bad-date',
      },
      degraded_reasons: [
        'command_retries_present',
        'command_dead_letters_present',
        'interaction_wait_stuck',
        'terminal_state_divergence',
        'terminal_reconciliation_unknown',
        'future_reason_code',
      ],
    };

    render(<ExecutionHealthPanel {...defaultProps} readiness={readiness} executionHealth={executionHealth} />);

    expect(screen.getByRole('alert')).toHaveTextContent('Execution blocked');
    for (const reason of [
      'task_service_unconfigured',
      'postgres_unavailable',
      'temporal_unavailable',
      'worker_unavailable',
      'dispatcher_unavailable',
      'dispatcher_backlog_stale',
      'command_retries_present',
      'command_dead_letters_present',
      'interaction_wait_stuck',
      'terminal_state_divergence',
      'terminal_reconciliation_unknown',
      'future_reason_code',
    ]) {
      expect(screen.getByText(reason)).toBeInTheDocument();
    }
    expect(screen.getByText('task-outbox')).toBeInTheDocument();
    expect(screen.getByText('task-wait')).toBeInTheDocument();
    expect(screen.getByText('task-reconcile')).toBeInTheDocument();
    expect(screen.getByText('Additional affected tasks are omitted.')).toBeInTheDocument();
    expect(screen.getAllByText('Unknown').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/Inspect readiness, metrics, and the affected task timeline/i)).toBeInTheDocument();
    expect(screen.getByText('2m')).toBeInTheDocument();
    expect(screen.getByText('2.0h')).toBeInTheDocument();
    expect(screen.getByText('30s')).toBeInTheDocument();
  });

  it('keeps operational signals visible when readiness fails', () => {
    render(
      <ExecutionHealthPanel
        {...defaultProps}
        readiness={undefined}
        readinessError={new Error('probe offline')}
      />,
    );

    expect(screen.getByText('Readiness unknown')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Readiness probe unavailable: probe offline');
    expect(screen.getByRole('heading', { name: 'Command Outbox' })).toBeInTheDocument();
  });

  it('keeps readiness visible when operational signals fail', () => {
    render(
      <ExecutionHealthPanel
        {...defaultProps}
        executionHealth={undefined}
        executionHealthError="offline"
      />,
    );

    expect(screen.getByText('Execution ready')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Operational signals unavailable: Request failed');
    expect(screen.getByText('Recovery guidance will appear when status data is available.')).toBeInTheDocument();
  });

  it('renders independent loading states without claiming health', () => {
    render(
      <ExecutionHealthPanel
        {...defaultProps}
        readiness={undefined}
        executionHealth={undefined}
        readinessLoading
        executionHealthLoading
      />,
    );

    expect(screen.getByText('Checking execution readiness')).toBeInTheDocument();
    expect(screen.getByText('Loading operational signals...')).toBeInTheDocument();
    expect(screen.queryByText('No degraded conditions reported.')).not.toBeInTheDocument();
  });

  it('invokes refresh once and exposes the in-progress state accessibly', () => {
    const onRefresh = vi.fn();
    const { rerender } = render(<ExecutionHealthPanel {...defaultProps} onRefresh={onRefresh} />);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh execution status' }));
    expect(onRefresh).toHaveBeenCalledTimes(1);

    rerender(<ExecutionHealthPanel {...defaultProps} onRefresh={onRefresh} refreshing />);
    const refreshingButton = screen.getByRole('button', { name: 'Refresh execution status' });
    expect(refreshingButton).toBeDisabled();
    expect(refreshingButton).toHaveTextContent('Refreshing...');
    fireEvent.click(refreshingButton);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});
