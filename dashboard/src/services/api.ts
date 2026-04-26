import { TaskSummarySnapshot, TaskStatus } from '../types/task';
import { SessionSnapshot, SessionStatus } from '../types/session';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
const API_SECRET_HEADER = 'X-Agent-Secret';

// For development, we can store the secret in localStorage or use an env var.
// SECURITY NOTE: Storing sensitive credentials like AGENT_SECRET in localStorage
// makes them vulnerable to XSS attacks. For a production-grade implementation,
// consider using HttpOnly cookies or a dedicated OAuth2/OIDC flow to manage sessions.
const getApiSecret = () => localStorage.getItem('AGENT_SECRET') || import.meta.env.VITE_API_SECRET || '';

async function fetchWithAuth(endpoint: string, options: RequestInit = {}) {
  const secret = getApiSecret();
  const headers = {
    'Content-Type': 'application/json',
    [API_SECRET_HEADER]: secret,
    ...options.headers,
  };

  const url = `${API_BASE_URL.replace(/\/$/, '')}/${endpoint.replace(/^\//, '')}`;
  const response = await fetch(url, {
    ...options,
    headers,
  });

  if (!response.ok) {
    let errorMessage = `API Error: ${response.status} ${response.statusText}`;
    try {
      const errorData = await response.json();
      if (errorData && errorData.detail) {
        errorMessage = typeof errorData.detail === 'string'
          ? errorData.detail
          : JSON.stringify(errorData.detail);
      }
    } catch (e) {
      // Fallback to default message if body is not JSON or doesn't have detail
    }
    throw new Error(errorMessage);
  }

  const text = await response.text();
  try {
    return text ? JSON.parse(text) : null;
  } catch (e) {
    console.error('Failed to parse API response as JSON:', e);
    return null;
  }
}

export const api = {
  async listTasks(): Promise<TaskSummarySnapshot[]> {
    try {
      return (await fetchWithAuth('/tasks')) || [];
    } catch (error) {
      if (import.meta.env.DEV) {
        console.warn('Failed to fetch tasks from API, falling back to mock data', error);
        return MOCK_TASKS;
      }
      throw error;
    }
  },

  async listSessions(): Promise<SessionSnapshot[]> {
    try {
      return (await fetchWithAuth('/sessions')) || [];
    } catch (error) {
      if (import.meta.env.DEV) {
        console.warn('Failed to fetch sessions from API, falling back to mock data', error);
        return MOCK_SESSIONS;
      }
      throw error;
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
