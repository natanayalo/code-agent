import React from 'react';
import { render, screen, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { SessionsPage } from './SessionsPage';
import { api } from '../services/api';
import { SessionStatus } from '../types/session';

// Mock the API service
vi.mock('../services/api', () => ({
  api: {
    listSessions: vi.fn(),
  },
}));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
    },
  },
});

describe('SessionsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryClient.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders loading state', () => {
    vi.mocked(api.listSessions).mockReturnValue(new Promise(() => {}));

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SessionsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(screen.getByText('Loading sessions...')).toBeInTheDocument();
  });

  it('renders short IDs without ellipsis when data is loaded', async () => {
    const mockSessions = [
      {
        session_id: 's1',
        user_id: 'u1',
        channel: 'http',
        external_thread_id: 't1',
        active_task_id: 'task-1',
        status: SessionStatus.ACTIVE,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ];

    vi.mocked(api.listSessions).mockResolvedValue(mockSessions);

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SessionsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByRole('heading', { name: /Sessions/i })).toBeInTheDocument();
    expect(await screen.findByText('ID: s1')).toBeInTheDocument();
    expect(screen.getByText('Active Task: task-1')).toBeInTheDocument();
    expect(screen.getByText(/u1/i)).toBeInTheDocument();
    expect(screen.getByText(/Channel: http/i)).toBeInTheDocument();
  });

  it('truncates long IDs with ellipsis', async () => {
    const mockSessions = [
      {
        session_id: '123456789abc',
        user_id: 'u2',
        channel: 'telegram',
        external_thread_id: 'thread-2',
        active_task_id: 'abcdefghijk',
        status: SessionStatus.ACTIVE,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ];

    vi.mocked(api.listSessions).mockResolvedValue(mockSessions);

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SessionsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText('ID: 12345678...')).toBeInTheDocument();
    expect(screen.getByText('Active Task: abcdefgh...')).toBeInTheDocument();
  });

  it('renders empty state when no sessions', async () => {
    vi.mocked(api.listSessions).mockResolvedValue([]);

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SessionsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText(/No sessions found/i)).toBeInTheDocument();
  });

  it('renders error state on failure', async () => {
    vi.mocked(api.listSessions).mockRejectedValue(new Error('Failed to fetch'));

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SessionsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText(/Error loading sessions/i)).toBeInTheDocument();
    expect(screen.getByText(/Failed to fetch/i)).toBeInTheDocument();
  });

  it('automatically refetches sessions every 30 seconds', async () => {
    vi.useFakeTimers();
    vi.mocked(api.listSessions).mockResolvedValue([
      {
        session_id: 's1',
        user_id: 'u1',
        channel: 'http',
        external_thread_id: 't1',
        status: SessionStatus.ACTIVE,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SessionsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await act(async () => {
      await Promise.resolve();
    });

    expect(api.listSessions).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30000);
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(api.listSessions).toHaveBeenCalledTimes(2);
  });

  it('handles sessions with missing or null IDs and optional fields', async () => {
    const mockSessions = [
      {
        session_id: 's-null-task',
        user_id: 'u-long-identifier-that-should-be-truncated-in-the-ui-to-prevent-layout-issues',
        channel: 'http',
        external_thread_id: 'thread-long-identifier-that-should-also-be-truncated',
        active_task_id: null,
        status: SessionStatus.CLOSED,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ];

    vi.mocked(api.listSessions).mockResolvedValue(mockSessions);

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SessionsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText(/u-long-identifier/i)).toBeInTheDocument();
    // Verify that Active Task label is not shown when task_id is null
    expect(screen.queryByText(/Active Task:/)).not.toBeInTheDocument();
    // Verify status class mapping (closed -> success)
    const badge = screen.getByText('closed');
    expect(badge.className).toContain('status-success');
  });
});
