import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import App from './App';
import { TaskStatus } from './types/task';
import { api } from './services/api';

// Mock the API service
vi.mock('./services/api', () => ({
  api: {
    listTasks: vi.fn(),
    getTask: vi.fn(),
    listSessions: vi.fn(),
    listPersonalMemory: vi.fn(),
    listProjectMemory: vi.fn(),
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
    window.history.pushState({}, '', '/');
  });

  it('renders without crashing and displays tasks when authenticated', async () => {
    const mockTasks = [
      { task_id: '1', task_text: 'Task 1', status: TaskStatus.COMPLETED, created_at: new Date().toISOString(), session_id: 's1', priority: 1, updated_at: new Date().toISOString() },
      { task_id: '2', task_text: 'Task 2', status: TaskStatus.FAILED, created_at: new Date().toISOString(), session_id: 's1', priority: 1, updated_at: new Date().toISOString() },
      { task_id: '3', task_text: 'Task 3', status: TaskStatus.CANCELLED, created_at: new Date().toISOString(), session_id: 's1', priority: 1, updated_at: new Date().toISOString() },
      { task_id: '4', task_text: 'Task 4', status: TaskStatus.PENDING, created_at: new Date().toISOString(), session_id: 's1', priority: 1, updated_at: new Date().toISOString() },
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

  it('shows TaskSpec and pending interactions in task detail from operator inbox', async () => {
    const createdAt = new Date().toISOString();
    vi.mocked(api.listTasks).mockResolvedValue([
      {
        task_id: 'task-clarify',
        task_text: 'Need repo clarification',
        status: TaskStatus.PENDING,
        created_at: createdAt,
        session_id: 's1',
        priority: 1,
        updated_at: createdAt,
        pending_interaction_count: 1,
      },
    ]);
    vi.mocked(api.getTask).mockResolvedValue({
      task_id: 'task-clarify',
      task_text: 'Need repo clarification',
      status: TaskStatus.PENDING,
      created_at: createdAt,
      session_id: 's1',
      priority: 1,
      updated_at: createdAt,
      task_spec: {
        goal: 'Clarify exact repository and file targets',
        assumptions: [],
        acceptance_criteria: ['Target files are explicitly listed'],
        non_goals: [],
        risk_level: 'low',
        task_type: 'investigation',
        allowed_actions: [],
        forbidden_actions: [],
        verification_commands: [],
        expected_artifacts: [],
        requires_clarification: true,
        clarification_questions: ['Which repository should be modified?'],
        requires_permission: false,
        delivery_mode: 'summary',
      },
      pending_interactions: [
        {
          interaction_id: 'hi-1',
          interaction_type: 'clarification',
          status: 'pending',
          summary: 'Task requires clarification before execution can continue.',
          data: {},
          response_data: null,
          created_at: createdAt,
          updated_at: createdAt,
        },
      ],
      timeline: [],
    });
    vi.mocked(api.auth.status).mockResolvedValue({ authenticated: true });

    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    );

    expect(await screen.findByText('Operator Inbox')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: /Need repo clarification/i }));

    expect(await screen.findByText('Task Detail')).toBeInTheDocument();
    expect(await screen.findByText('Clarify exact repository and file targets')).toBeInTheDocument();
    expect(await screen.findByText('Task requires clarification before execution can continue.')).toBeInTheDocument();
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

  it('renders settings placeholder when visiting /settings', async () => {
    window.history.pushState({}, '', '/settings');
    vi.mocked(api.auth.status).mockResolvedValue({ authenticated: true });

    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    );

    expect(await screen.findByRole('heading', { name: /Settings coming soon/i })).toBeInTheDocument();
    expect(screen.getByText(/Configuration controls are not available yet/i)).toBeInTheDocument();
  });

  it('renders knowledge base page when visiting /knowledge-base', async () => {
    window.history.pushState({}, '', '/knowledge-base');
    vi.mocked(api.auth.status).mockResolvedValue({ authenticated: true });
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);

    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    );

    expect(await screen.findByRole('heading', { name: /Knowledge Base/i })).toBeInTheDocument();
    expect(
      screen.getByText(/Manage skeptical memory entries with confidence and verification metadata/i)
    ).toBeInTheDocument();
  });
});
