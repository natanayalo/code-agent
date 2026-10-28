import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2 } from 'lucide-react';
import { api } from '../services/api';
import { ReadinessAlertDeliverySnapshot, ReadinessAlertStateSnapshot } from '../types/metrics';

function timestamp(value: string | null | undefined): string {
  if (!value) return 'Not available';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Unknown' : date.toLocaleString();
}

function StateList({ states, checkedAt }: { states: ReadinessAlertStateSnapshot[]; checkedAt: number }) {
  return <section className="metric-detail-card card" aria-labelledby="readiness-alert-states">
    <h3 id="readiness-alert-states">Persisted readiness reasons</h3>
    <p className="metric-note">Last checked: {timestamp(new Date(checkedAt).toISOString())}</p>
    {states.length === 0 ? <p>No readiness reasons have been recorded.</p> : <ul className="ops-list">
      {states.map((state) => <li key={state.reason}>
        <strong>{state.degraded ? <AlertTriangle size={15} aria-label="Degraded" /> : <CheckCircle2 size={15} aria-label="Recovered" />} {state.reason}</strong>
        <span>{state.degraded ? 'Degraded' : 'Recovered'} · transition {state.transition_number}</span>
        <small>Last transition: {timestamp(state.last_transition_at)} · reason checked: {timestamp(state.last_checked_at)}</small>
      </li>)}
    </ul>}
  </section>;
}

function DeliveryList({ deliveries, checkedAt }: { deliveries: ReadinessAlertDeliverySnapshot[]; checkedAt: number }) {
  return <section className="metric-detail-card card" aria-labelledby="readiness-alert-deliveries">
    <h3 id="readiness-alert-deliveries">Recent notification deliveries</h3>
    <p className="metric-note">Last checked: {timestamp(new Date(checkedAt).toISOString())}</p>
    {deliveries.length === 0 ? <p>No notification deliveries recorded.</p> : <ul className="ops-list">
      {deliveries.map((delivery) => <li key={delivery.id}>
        <strong>{delivery.reason} · {delivery.transition}</strong>
        <span>Status: {delivery.status} · attempts: {delivery.attempts}</span>
        <small>Created: {timestamp(delivery.created_at)} · next attempt: {timestamp(delivery.next_attempt_at)}{delivery.delivered_at ? ` · delivered: ${timestamp(delivery.delivered_at)}` : ''}</small>
        {delivery.last_failure_code || delivery.last_http_status ? <small>Last failure: {delivery.last_failure_code ?? 'unknown'}{delivery.last_http_status ? ` · HTTP ${delivery.last_http_status}` : ''}</small> : null}
      </li>)}
    </ul>}
  </section>;
}

export function ReadinessAlertsSection() {
  const states = useQuery({ queryKey: ['readiness-alert-states'], queryFn: api.listReadinessAlertStates, refetchInterval: 60000 });
  const deliveries = useQuery({ queryKey: ['readiness-alert-deliveries'], queryFn: () => api.listReadinessAlertDeliveries(), refetchInterval: 60000 });
  return <div className="metrics-details-grid" aria-label="Readiness alert history">
    <section className="metric-detail-card card" aria-label="Readiness reason state data status">
      {states.isLoading ? <p>Loading readiness reason history…</p> : states.error ? <p role="alert">Readiness reason history unavailable: {(states.error as Error).message}</p> : <StateList states={states.data ?? []} checkedAt={states.dataUpdatedAt} />}
    </section>
    <section className="metric-detail-card card" aria-label="Notification delivery data status">
      {deliveries.isLoading ? <p>Loading notification deliveries…</p> : deliveries.error ? <p role="alert">Notification deliveries unavailable: {(deliveries.error as Error).message}</p> : <DeliveryList deliveries={deliveries.data ?? []} checkedAt={deliveries.dataUpdatedAt} />}
    </section>
  </div>;
}
