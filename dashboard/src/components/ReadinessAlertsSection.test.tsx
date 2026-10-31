import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../services/api';
import { ReadinessAlertsSection } from './ReadinessAlertsSection';

vi.mock('../services/api', () => ({ api: {
  listReadinessAlertStates: vi.fn(),
  listReadinessAlertDeliveries: vi.fn(),
  acknowledgeReadinessIncident: vi.fn(),
} }));

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><ReadinessAlertsSection /></QueryClientProvider>);
}

describe('ReadinessAlertsSection', () => {
  it('shows distinct worker capability incidents and fixed actions', async () => {
    vi.mocked(api.listReadinessAlertStates).mockResolvedValue([{ reason: 'provider_capability', worker_node_id: 'worker-id', provider: 'codex', capability: 'credentials', next_action: 'Check credentials.', degraded: true, transition_number: 1, last_transition_at: '2026-10-27T12:00:00Z', last_checked_at: '2026-10-27T12:01:00Z' }]);
    vi.mocked(api.listReadinessAlertDeliveries).mockResolvedValue([]);
    mount();
    expect(await screen.findByText(/worker worker-id · codex · credentials/)).toBeInTheDocument();
    expect(screen.getByText('Next action: Check credentials.')).toBeInTheDocument();
  });

  it('acknowledges only the active incident and refreshes its state', async () => {
    const active = { reason: 'interaction_wait_stuck', task_id: 'task-42', acknowledgment_reason: 'interaction_wait_stuck|task-42', degraded: true, transition_number: 3, last_transition_at: '2026-10-27T12:00:00Z', last_checked_at: '2026-10-27T12:01:00Z' };
    vi.mocked(api.listReadinessAlertStates).mockResolvedValueOnce([active]).mockResolvedValue([
      { ...active, acknowledged_at: '2026-10-27T12:02:00Z', acknowledged_by: 'operator' },
    ]);
    vi.mocked(api.listReadinessAlertDeliveries).mockResolvedValue([]);
    vi.mocked(api.acknowledgeReadinessIncident).mockResolvedValue({ reason: active.acknowledgment_reason, transition_number: 3, acknowledged_at: '2026-10-27T12:02:00Z', acknowledged_by: 'operator' });
    mount();

    fireEvent.click(await screen.findByRole('button', { name: 'Acknowledge' }));

    await waitFor(() => expect(api.acknowledgeReadinessIncident).toHaveBeenCalledWith('interaction_wait_stuck|task-42', 3));
    expect(await screen.findByText(/Acknowledged by operator:/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Acknowledge' })).not.toBeInTheDocument();
  });
  beforeEach(() => vi.clearAllMocks());

  it('shows independent loading states', () => {
    vi.mocked(api.listReadinessAlertStates).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.listReadinessAlertDeliveries).mockReturnValue(new Promise(() => {}));
    mount();
    expect(screen.getByText('Loading readiness reason history…')).toBeInTheDocument();
    expect(screen.getByText('Loading notification deliveries…')).toBeInTheDocument();
  });

  it('shows empty and populated source results independently', async () => {
    vi.mocked(api.listReadinessAlertStates).mockResolvedValue([{ reason: 'interaction_wait_stuck', task_id: 'task-42', degraded: true, transition_number: 3, last_transition_at: '2026-10-27T12:00:00Z', last_checked_at: '2026-10-27T12:01:00Z' }]);
    vi.mocked(api.listReadinessAlertDeliveries).mockResolvedValue([{ id: 'interaction-1', reason: 'interaction_wait_stuck', task_id: 'task-42', transition_number: 3, transition: 'degraded', status: 'delivered', attempts: 1, next_attempt_at: '2026-10-27T12:01:00Z', delivered_at: '2026-10-27T12:01:01Z', last_failure_code: null, last_http_status: null, created_at: '2026-10-27T12:00:00Z' }]);
    mount();
    expect(await screen.findAllByText(/interaction_wait_stuck/)).toHaveLength(2);
    expect(screen.getAllByText(/task task-42/)).toHaveLength(2);
    expect(screen.getAllByText(/Last checked:/)).toHaveLength(2);
  });

  it('shows retry metadata and source-specific failures', async () => {
    vi.mocked(api.listReadinessAlertStates).mockRejectedValue(new Error('database unavailable'));
    vi.mocked(api.listReadinessAlertDeliveries).mockResolvedValue([{ id: 'd1', reason: 'temporal_unavailable', transition_number: 1, transition: 'degraded', status: 'retrying', attempts: 2, next_attempt_at: '2026-10-27T12:05:00Z', delivered_at: null, last_failure_code: 'timeout', last_http_status: 503, created_at: '2026-10-27T12:00:00Z' }]);
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('database unavailable');
    expect(await screen.findByText('Status: retrying · attempts: 2')).toBeInTheDocument();
    expect(screen.getByText('Last failure: timeout · HTTP 503')).toBeInTheDocument();
  });

  it('renders recovered states, delivery success, and invalid timestamps safely', async () => {
    vi.mocked(api.listReadinessAlertStates).mockResolvedValue([{ reason: 'temporal_unavailable', degraded: false, transition_number: 4, last_transition_at: 'bad-date', last_checked_at: '2026-10-27T12:01:00Z' }]);
    vi.mocked(api.listReadinessAlertDeliveries).mockResolvedValue([{ id: 'd2', reason: 'temporal_unavailable', transition_number: 4, transition: 'recovered', status: 'delivered', attempts: 1, next_attempt_at: '2026-10-27T12:02:00Z', delivered_at: '2026-10-27T12:02:01Z', last_failure_code: null, last_http_status: null, created_at: '2026-10-27T12:02:00Z' }]);
    mount();
    expect(await screen.findByText('Recovered · transition 4')).toBeInTheDocument();
    expect(screen.getByText(/Last transition: Unknown/)).toBeInTheDocument();
    expect(screen.getByText(/delivered:/)).toBeInTheDocument();
    expect(screen.queryByText(/Last failure:/)).not.toBeInTheDocument();
  });

  it('shows a failure in the delivery source without hiding states', async () => {
    vi.mocked(api.listReadinessAlertStates).mockResolvedValue([]);
    vi.mocked(api.listReadinessAlertDeliveries).mockRejectedValue(new Error('delivery store unavailable'));
    mount();
    expect(await screen.findByText('No readiness reasons have been recorded.')).toBeInTheDocument();
    expect(await screen.findByRole('alert')).toHaveTextContent('delivery store unavailable');
  });
});
