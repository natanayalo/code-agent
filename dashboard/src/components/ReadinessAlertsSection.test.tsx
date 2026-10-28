import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../services/api';
import { ReadinessAlertsSection } from './ReadinessAlertsSection';

vi.mock('../services/api', () => ({ api: {
  listReadinessAlertStates: vi.fn(),
  listReadinessAlertDeliveries: vi.fn(),
} }));

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><ReadinessAlertsSection /></QueryClientProvider>);
}

describe('ReadinessAlertsSection', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows independent loading states', () => {
    vi.mocked(api.listReadinessAlertStates).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.listReadinessAlertDeliveries).mockReturnValue(new Promise(() => {}));
    mount();
    expect(screen.getByText('Loading readiness reason history…')).toBeInTheDocument();
    expect(screen.getByText('Loading notification deliveries…')).toBeInTheDocument();
  });

  it('shows empty and populated source results independently', async () => {
    vi.mocked(api.listReadinessAlertStates).mockResolvedValue([{ reason: 'temporal_unavailable', degraded: true, transition_number: 3, last_transition_at: '2026-10-27T12:00:00Z', last_checked_at: '2026-10-27T12:01:00Z' }]);
    vi.mocked(api.listReadinessAlertDeliveries).mockResolvedValue([]);
    mount();
    expect(await screen.findByText(/temporal_unavailable/)).toBeInTheDocument();
    expect(screen.getByText('No notification deliveries recorded.')).toBeInTheDocument();
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
