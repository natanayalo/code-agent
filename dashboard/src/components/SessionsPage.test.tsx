import React from 'react';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
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

  it('renders sessions when data is loaded', async () => {
    const mockSessions = [
      {
        session_id: 's1',
        user_id: 'u1',
        channel: 'http',
        external_thread_id: 't1',
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
    expect(await screen.findByText(/ID: s1/i)).toBeInTheDocument();
    expect(screen.getByText(/u1/i)).toBeInTheDocument();
    expect(screen.getByText(/Channel: http/i)).toBeInTheDocument();
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
});
