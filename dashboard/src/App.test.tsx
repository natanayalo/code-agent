import React from 'react';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import App from './App';
import { TaskStatus } from './types/task';
import { api } from './services/api';

// Mock the API service
vi.mock('./services/api', () => ({
  api: {
    listTasks: vi.fn(),
    listSessions: vi.fn(),
    getMetrics: vi.fn(),
    auth: {
      status: vi.fn(),
    },
  },
}));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
    },
  },
});

describe('App', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryClient.clear();
  });

  it('renders without crashing and displays tasks when authenticated', async () => {
    const mockTasks = [
      { task_id: '1', task_text: 'Task 1', status: TaskStatus.COMPLETED, created_at: new Date().toISOString(), session_id: 's1', priority: 1, updated_at: new Date().toISOString() },
      { task_id: '2', task_text: 'Task 2', status: TaskStatus.FAILED, created_at: new Date().toISOString(), session_id: 's1', priority: 1, updated_at: new Date().toISOString() },
      { task_id: '3', task_text: 'Task 3', status: TaskStatus.CANCELLED, created_at: new Date().toISOString(), session_id: 's1', priority: 1, updated_at: new Date().toISOString() },
    ];

    vi.mocked(api.listTasks).mockResolvedValue(mockTasks);
    vi.mocked(api.auth.status).mockResolvedValue({ authenticated: true });

    const { container } = render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    );

    // Wait for the Task Status Board to appear (after auth check)
    expect(await screen.findByText('Task Status Board')).toBeInTheDocument();
    expect(await screen.findByText('Task 1')).toBeInTheDocument();

    const statsValues = container.querySelectorAll('.stats-value');
    expect(statsValues[0]).toHaveTextContent('1'); // Completed
    expect(statsValues[1]).toHaveTextContent('2'); // Failed + Cancelled
  });

  it('renders login page when not authenticated', async () => {
    vi.mocked(api.auth.status).mockResolvedValue({ authenticated: false });

    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    );

    expect(await screen.findByText('Agent Dashboard')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('••••••••••••••••')).toBeInTheDocument();
  });
});
