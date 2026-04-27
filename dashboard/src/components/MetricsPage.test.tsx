import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { MetricsPage } from './MetricsPage';
import { api } from '../services/api';

// Mock the API service
vi.mock('../services/api', () => ({
  api: {
    getMetrics: vi.fn(),
  },
}));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
    },
  },
});

describe('MetricsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryClient.clear();
  });

  it('renders loading state', () => {
    vi.mocked(api.getMetrics).mockReturnValue(new Promise(() => {}));

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <MetricsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(screen.getByText('Loading metrics...')).toBeInTheDocument();
  });

  it('renders metrics when data is loaded', async () => {
    const mockMetrics = {
      total_tasks: 100,
      retried_tasks: 10,
      retry_rate: 0.1,
      status_counts: { completed: 80, failed: 20 },
      worker_usage: { gemini: 60, codex: 40 },
      avg_duration_seconds: 45.5,
      success_rate: 0.8,
    };

    vi.mocked(api.getMetrics).mockResolvedValue(mockMetrics);

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <MetricsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByRole('heading', { name: /Operational Metrics/i })).toBeInTheDocument();
    expect(await screen.findByText('100')).toBeInTheDocument(); // Total tasks
    expect(screen.getByText('80.0%')).toBeInTheDocument(); // Success rate
    expect(screen.getByText('45.5s')).toBeInTheDocument(); // Avg duration
    expect(screen.getByText('10.0%')).toBeInTheDocument(); // Retry rate

    expect(screen.getByText(/completed/i)).toBeInTheDocument();
    expect(screen.getByText('80')).toBeInTheDocument();
    expect(screen.getByText(/gemini/i)).toBeInTheDocument();
    expect(screen.getByText(/60 runs/i)).toBeInTheDocument();
    expect(screen.getByText(/codex/i)).toBeInTheDocument();
    expect(screen.getByText(/40 runs/i)).toBeInTheDocument();
  });

  it('renders low success rate with failure color', async () => {
    const lowMetrics = {
      total_tasks: 10,
      retried_tasks: 0,
      retry_rate: 0,
      status_counts: { failed: 10 },
      worker_usage: {},
      avg_duration_seconds: 0,
      success_rate: 0.1, // < 0.8 threshold
    };

    vi.mocked(api.getMetrics).mockResolvedValue(lowMetrics);

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <MetricsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    const successRateText = await screen.findByText('Success Rate');
    const successCard = successRateText.closest('.metric-summary-card');
    const icon = successCard?.querySelector('svg');
    // Success rate is 0.1, which is below 0.8 threshold, should use failure color
    // Lucide icons map the color prop to the stroke attribute on the SVG
    expect(icon).toHaveAttribute('stroke', 'var(--color-status-failed)');
  });

  it('renders error state on failure', async () => {
    vi.mocked(api.getMetrics).mockRejectedValue(new Error('Failed to fetch'));

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <MetricsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText(/Error loading metrics/i)).toBeInTheDocument();
    expect(screen.getByText(/Failed to fetch/i)).toBeInTheDocument();
  });

  it('retries fetching metrics when Retry button is clicked', async () => {
    vi.mocked(api.getMetrics).mockRejectedValueOnce(new Error('First fail'));

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <MetricsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText(/Error loading metrics/i)).toBeInTheDocument();

    vi.mocked(api.getMetrics).mockResolvedValueOnce({
      total_tasks: 5,
      retried_tasks: 0,
      retry_rate: 0,
      status_counts: {},
      worker_usage: {},
      avg_duration_seconds: 0,
      success_rate: 1,
    });

    const retryButton = screen.getByText('Retry');
    fireEvent.click(retryButton);

    expect(api.getMetrics).toHaveBeenCalledTimes(2);
    expect(await screen.findByText('Operational Metrics')).toBeInTheDocument();
  });
});
