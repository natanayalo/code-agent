import { TaskSummarySnapshot, TaskStatus } from '../types/task';
import { SessionSnapshot, SessionStatus } from '../types/session';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
const API_SECRET_HEADER = 'X-Agent-Secret';

// For development, we can store the secret in localStorage or use an env var
const getApiSecret = () => localStorage.getItem('AGENT_SECRET') || import.meta.env.VITE_API_SECRET || '';

async function fetchWithAuth(endpoint: string, options: RequestInit = {}) {
  const secret = getApiSecret();
  const headers = {
    'Content-Type': 'application/json',
    [API_SECRET_HEADER]: secret,
    ...options.headers,
  };

  const response = await fetch(`${API_BASE_URL}${endpoint}`, {
    ...options,
    headers,
  });

  if (!response.ok) {
    throw new Error(`API Error: ${response.status} ${response.statusText}`);
  }

  return response.json();
}

export const api = {
  async listTasks(): Promise<TaskSummarySnapshot[]> {
    try {
      return await fetchWithAuth('/tasks');
    } catch (error) {
      console.warn('Failed to fetch tasks from API, using mock data', error);
      return MOCK_TASKS;
    }
  },

  async listSessions(): Promise<SessionSnapshot[]> {
    try {
      return await fetchWithAuth('/sessions');
    } catch (error) {
      console.warn('Failed to fetch sessions from API, using mock data', error);
      return MOCK_SESSIONS;
    }
  },
};

// --- Mock Data for Development ---

const MOCK_TASKS: TaskSummarySnapshot[] = [
  {
    task_id: 'task-1',
    session_id: 'session-1',
    status: TaskStatus.IN_PROGRESS,
    task_text: 'Implement PWA Frontend Architecture',
    created_at: new Date(Date.now() - 3600000).toISOString(),
    updated_at: new Date().toISOString(),
    latest_run_status: 'running',
    latest_run_worker: 'codex',
  },
  {
    task_id: 'task-2',
    session_id: 'session-1',
    status: TaskStatus.COMPLETED,
    task_text: 'Add API endpoints for session listing',
    created_at: new Date(Date.now() - 7200000).toISOString(),
    updated_at: new Date(Date.now() - 3600000).toISOString(),
    latest_run_status: 'success',
    latest_run_worker: 'gemini',
  },
  {
    task_id: 'task-3',
    session_id: 'session-2',
    status: TaskStatus.FAILED,
    task_text: 'Debug race condition in task scheduler',
    created_at: new Date(Date.now() - 86400000).toISOString(),
    updated_at: new Date(Date.now() - 86000000).toISOString(),
    latest_run_status: 'error',
    latest_run_worker: 'codex',
  },
  {
    task_id: 'task-4',
    session_id: 'session-1',
    status: TaskStatus.PENDING,
    task_text: 'Refactor orchestrator state management',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  }
];

const MOCK_SESSIONS: SessionSnapshot[] = [
  {
    session_id: 'session-1',
    user_id: 'user-1',
    channel: 'telegram',
    external_thread_id: 'thread-123',
    status: SessionStatus.ACTIVE,
    created_at: new Date(Date.now() - 86400000).toISOString(),
    updated_at: new Date().toISOString(),
  },
  {
    session_id: 'session-2',
    user_id: 'user-1',
    channel: 'http',
    external_thread_id: 'http-thread',
    status: SessionStatus.CLOSED,
    created_at: new Date(Date.now() - 172800000).toISOString(),
    updated_at: new Date(Date.now() - 86400000).toISOString(),
  }
];
