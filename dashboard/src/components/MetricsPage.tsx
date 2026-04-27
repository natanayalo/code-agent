import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../services/api';
import { DashboardLayout } from './layout/DashboardLayout';
import { Activity, Clock, Database, Cpu, TrendingUp } from 'lucide-react';

const METRICS_REFETCH_INTERVAL_MS = 60000;
const SUCCESS_RATE_HEALTHY_THRESHOLD = 0.8;

export function MetricsPage() {
  const {
    data: metrics,
    isLoading,
    error,
    refetch
  } = useQuery({
    queryKey: ['metrics'],
    queryFn: () => api.getMetrics(),
    refetchInterval: METRICS_REFETCH_INTERVAL_MS,
  });

  if (error) {
    return (
      <DashboardLayout>
        <div className="error-container">
          <h2>Error loading metrics</h2>
          <p>{(error as Error).message}</p>
          <button onClick={() => refetch()} className="btn-primary">Retry</button>
        </div>
      </DashboardLayout>
    );
  }

  return (
    <DashboardLayout>
      <div className="page-header">
        <h1>Operational Metrics</h1>
        <p className="page-subtitle">Service health and execution performance (last 24h)</p>
      </div>

      {isLoading || !metrics ? (
        <div className="loading-container">
          <div className="spinner"></div>
          <p>Loading metrics...</p>
        </div>
      ) : (
        <div className="metrics-container">
          <div className="metrics-summary-grid">
            <MetricCard
              icon={<Database size={24} color="var(--color-accent-primary)" />}
              label="Total Tasks"
              value={metrics.total_tasks.toString()}
            />
            <MetricCard
              icon={<TrendingUp size={24} color={metrics.success_rate >= SUCCESS_RATE_HEALTHY_THRESHOLD ? 'var(--color-status-completed)' : 'var(--color-status-failed)'} />}
              label="Success Rate"
              value={`${(metrics.success_rate * 100).toFixed(1)}%`}
            />
            <MetricCard
              icon={<Clock size={24} color="var(--color-accent-secondary)" />}
              label="Avg Duration"
              value={`${metrics.avg_duration_seconds.toFixed(1)}s`}
            />
            <MetricCard
              icon={<Activity size={24} color="var(--color-status-failed)" />}
              label="Retry Rate"
              value={`${(metrics.retry_rate * 100).toFixed(1)}%`}
            />
          </div>

          <div className="metrics-details-grid">
            <div className="metric-detail-card card">
              <h3>Status Distribution</h3>
              <div className="status-list">
                {Object.entries(metrics.status_counts).map(([status, count]) => (
                  <div key={status} className="status-item">
                    <span className={`status-dot status-${status.toLowerCase()}`}></span>
                    <span className="status-label">{status}</span>
                    <span className="status-count">{count}</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="metric-detail-card card">
              <h3>Worker Usage</h3>
              <div className="worker-list">
                {Object.entries(metrics.worker_usage).map(([worker, count]) => (
                  <div key={worker} className="worker-item">
                    <Cpu size={16} />
                    <span className="worker-label">{worker}</span>
                    <span className="worker-count">{count} runs</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </DashboardLayout>
  );
}

interface MetricCardProps {
  icon: React.ReactNode;
  label: string;
  value: string;
}

function MetricCard({ icon, label, value }: MetricCardProps) {
  return (
    <div className="metric-summary-card card">
      <div className="metric-icon">{icon}</div>
      <div className="metric-content">
        <span className="metric-label">{label}</span>
        <span className="metric-value">{value}</span>
      </div>
    </div>
  );
}
