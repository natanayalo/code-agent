import { TaskSummarySnapshot } from '../types/task';
import { SessionSnapshot } from '../types/session';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
const API_SECRET_HEADER = 'X-Webhook-Token';

// For development, we can store the secret in localStorage.
// SECURITY NOTE:
// 1. Storing sensitive credentials in localStorage makes them vulnerable to XSS.
// 2. Do not embed secrets in VITE_ env vars, which are compiled into the client bundle.
// This implementation is for DEVELOPMENT ONLY. For production, use HttpOnly cookies
// or an OAuth2/OIDC flow as tracked in T-136.
const getApiSecret = () => {
  if (import.meta.env.PROD) return '';
  return localStorage.getItem('AGENT_SECRET') || '';
};

async function fetchWithAuth(endpoint: string, options: RequestInit = {}) {
  const secret = getApiSecret();
  const headers = {
    'Content-Type': 'application/json',
    [API_SECRET_HEADER]: secret,
    ...options.headers,
  };

  const baseUrl = API_BASE_URL.endsWith('/') ? API_BASE_URL : `${API_BASE_URL}/`;
  const url = new URL(endpoint.replace(/^\//, ''), baseUrl).toString();
  const response = await fetch(url, {
    ...options,
    headers,
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
      // Fallback to default message if body is not JSON or doesn't have detail
    }
    throw new Error(errorMessage);
  }

  if (response.status === 204) {
    return null;
  }

  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    throw new Error(`Expected JSON response but received content-type: ${contentType || 'unknown'}`);
  }

  try {
    return await response.json();
  } catch {
    throw new Error('Failed to parse server response as JSON');
  }
}

export const api = {
  async listTasks(): Promise<TaskSummarySnapshot[]> {
    try {
      const data = await fetchWithAuth('/tasks');
      return Array.isArray(data) ? data : [];
    } catch (error) {
      console.warn('Failed to fetch tasks from API', error);
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
};

// --- Mock Data for Development ---
// (Mocks removed to prevent masking integration issues)
