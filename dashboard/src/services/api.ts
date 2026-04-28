import { TaskReplayRequest, TaskSummarySnapshot, TaskSnapshot } from '../types/task';
import { SessionSnapshot } from '../types/session';
import { OperationalMetrics } from '../types/metrics';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api';

async function fetchWithAuth(endpoint: string, options: RequestInit = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers,
  };

  const baseUrl = API_BASE_URL.endsWith('/') ? API_BASE_URL : `${API_BASE_URL}/`;
  const url = new URL(endpoint.replace(/^\//, ''), new URL(baseUrl, window.location.origin)).toString();

  const response = await fetch(url, {
    ...options,
    headers,
    credentials: 'include', // Enable HttpOnly cookies
  });

  if (!response.ok) {
    let errorMessage = `API Error: ${response.status} ${response.statusText}`;
    const contentType = response.headers.get('content-type') || '';
    try {
      if (contentType.includes('application/json')) {
        const errorData = await response.json();
        if (errorData && errorData.detail) {
          errorMessage = typeof errorData.detail === 'string'
            ? errorData.detail
            : JSON.stringify(errorData.detail);
        }
      }
    } catch {
      // Fallback to default message
    }
    throw new Error(errorMessage);
  }

  if (response.status === 204) {
    return null;
  }

  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    if (response.status === 204) return null;
    throw new Error(`Expected JSON response but received content-type: ${contentType || 'unknown'}`);
  }

  try {
    return await response.json();
  } catch {
    throw new Error('Failed to parse server response as JSON');
  }
}

export const api = {
  auth: {
    async login(secret: string): Promise<{ status: string }> {
      return await fetchWithAuth('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ secret }),
      });
    },

    async logout(): Promise<void> {
      await fetchWithAuth('/auth/logout', { method: 'POST' });
    },

    async status(): Promise<{ authenticated: boolean }> {
      return await fetchWithAuth('/auth/status');
    },
  },

  async listTasks(): Promise<TaskSummarySnapshot[]> {
    try {
      const data = await fetchWithAuth('/tasks');
      return Array.isArray(data) ? data : [];
    } catch (error) {
      console.warn('Failed to fetch tasks from API', error);
      throw error;
    }
  },

  async getTask(taskId: string): Promise<TaskSnapshot> {
    try {
      return await fetchWithAuth(`/tasks/${taskId}`);
    } catch (error) {
      console.warn(`Failed to fetch task ${taskId}`, error);
      throw error;
    }
  },

  async listSessions(): Promise<SessionSnapshot[]> {
    try {
      const data = await fetchWithAuth('/sessions');
      return Array.isArray(data) ? data : [];
    } catch (error) {
      console.warn('Failed to fetch sessions from API', error);
      throw error;
    }
  },

  async decideTaskApproval(taskId: string, approved: boolean): Promise<unknown> {
    try {
      return await fetchWithAuth(`/tasks/${taskId}/approval`, {
        method: 'POST',
        body: JSON.stringify({ approved }),
      });
    } catch (error) {
      console.warn(`Failed to ${approved ? 'approve' : 'reject'} task ${taskId}`, error);
      throw error;
    }
  },

  async replayTask(taskId: string, replayRequest?: TaskReplayRequest): Promise<TaskSnapshot> {
    try {
      return await fetchWithAuth(`/tasks/${taskId}/replay`, {
        method: 'POST',
        ...(replayRequest ? { body: JSON.stringify(replayRequest) } : {}),
      });
    } catch (error) {
      console.warn(`Failed to replay task ${taskId}`, error);
      throw error;
    }
  },

  async getMetrics(windowHours: number = 24): Promise<OperationalMetrics> {
    try {
      return await fetchWithAuth(`/metrics?window_hours=${windowHours}`);
    } catch (error) {
      console.warn('Failed to fetch metrics from API', error);
      throw error;
    }
  },
};

// --- Mock Data for Development ---
// (Mocks removed to prevent masking integration issues)
