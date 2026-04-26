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

  it('renders without crashing and displays tasks', async () => {
    const mockTasks = [
      { task_id: '1', task_text: 'Task 1', status: TaskStatus.COMPLETED, created_at: new Date().toISOString(), session_id: 's1', priority: 1, updated_at: new Date().toISOString() },
      { task_id: '2', task_text: 'Task 2', status: TaskStatus.FAILED, created_at: new Date().toISOString(), session_id: 's1', priority: 1, updated_at: new Date().toISOString() },
      { task_id: '3', task_text: 'Task 3', status: TaskStatus.CANCELLED, created_at: new Date().toISOString(), session_id: 's1', priority: 1, updated_at: new Date().toISOString() },
    ];

    vi.mocked(api.listTasks).mockResolvedValue(mockTasks);

    const { container } = render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    );

    expect(screen.getByText('Task Status Board')).toBeInTheDocument();
    // Use findByText for async data
    expect(await screen.findByText('Task 1')).toBeInTheDocument();

    const statsValues = container.querySelectorAll('.stats-value');
    expect(statsValues[0]).toHaveTextContent('1'); // Completed
    expect(statsValues[1]).toHaveTextContent('2'); // Failed + Cancelled
  });
});
