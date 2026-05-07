import { TaskReplayRequest, TaskSummarySnapshot, TaskSnapshot } from '../types/task';
import { SessionSnapshot } from '../types/session';
import { OperationalMetrics } from '../types/metrics';
import {
  PersonalMemorySnapshot,
  PersonalMemoryUpsertRequest,
  ProjectMemorySnapshot,
  ProjectMemoryUpsertRequest,
} from '../types/memory';
import { ToolDefinition, SandboxStatusResponse } from '../types/system';

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

  async listPersonalMemory(
    userId: string,
    limit?: number,
    offset?: number,
  ): Promise<PersonalMemorySnapshot[]> {
    try {
      const query = new URLSearchParams({ user_id: userId });
      if (typeof limit === 'number') {
        query.set('limit', String(limit));
      }
      if (typeof offset === 'number') {
        query.set('offset', String(offset));
      }
      const data = await fetchWithAuth(`/knowledge-base/personal?${query.toString()}`);
      return Array.isArray(data) ? data : [];
    } catch (error) {
      console.warn('Failed to fetch personal memory from API', error);
      throw error;
    }
  },

  async upsertPersonalMemory(
    payload: PersonalMemoryUpsertRequest,
  ): Promise<PersonalMemorySnapshot> {
    try {
      return await fetchWithAuth('/knowledge-base/personal', {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
    } catch (error) {
      console.warn('Failed to upsert personal memory entry', error);
      throw error;
    }
  },

  async deletePersonalMemory(userId: string, memoryKey: string): Promise<void> {
    try {
      const query = new URLSearchParams({
        user_id: userId,
        memory_key: memoryKey,
      });
      await fetchWithAuth(`/knowledge-base/personal?${query.toString()}`, {
        method: 'DELETE',
      });
    } catch (error) {
      console.warn('Failed to delete personal memory entry', error);
      throw error;
    }
  },

  async listProjectMemory(
    repoUrl?: string,
    limit?: number,
    offset?: number,
  ): Promise<ProjectMemorySnapshot[]> {
    try {
      const query = new URLSearchParams();
      if (repoUrl) {
        query.set('repo_url', repoUrl);
      }
      if (typeof limit === 'number') {
        query.set('limit', String(limit));
      }
      if (typeof offset === 'number') {
        query.set('offset', String(offset));
      }
      const queryString = query.toString();
      const data = await fetchWithAuth(
        `/knowledge-base/project${queryString.length > 0 ? `?${queryString}` : ''}`
      );
      return Array.isArray(data) ? data : [];
    } catch (error) {
      console.warn('Failed to fetch project memory from API', error);
      throw error;
    }
  },

  async upsertProjectMemory(payload: ProjectMemoryUpsertRequest): Promise<ProjectMemorySnapshot> {
    try {
      return await fetchWithAuth('/knowledge-base/project', {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
    } catch (error) {
      console.warn('Failed to upsert project memory entry', error);
      throw error;
    }
  },

  async deleteProjectMemory(repoUrl: string, memoryKey: string): Promise<void> {
    try {
      const query = new URLSearchParams({
        repo_url: repoUrl,
        memory_key: memoryKey,
      });
      await fetchWithAuth(`/knowledge-base/project?${query.toString()}`, {
        method: 'DELETE',
      });
    } catch (error) {
      console.warn('Failed to delete project memory entry', error);
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

  async respondToInteraction(
    taskId: string,
    interactionId: string,
    status: string,
    responseData: Record<string, unknown> = {}
  ): Promise<TaskSnapshot> {
    try {
      return await fetchWithAuth(`/tasks/${taskId}/interactions/${interactionId}/response`, {
        method: 'POST',
        body: JSON.stringify({ status, response_data: responseData }),
      });
    } catch (error) {
      console.warn(`Failed to respond to interaction ${interactionId} for task ${taskId}`, error);
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

  async cancelTask(taskId: string): Promise<TaskSnapshot> {
    try {
      return await fetchWithAuth(`/tasks/${taskId}/cancel`, {
        method: 'POST',
      });
    } catch (error) {
      console.warn(`Failed to cancel task ${taskId}`, error);
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

  async getSystemTools(): Promise<ToolDefinition[]> {
    try {
      const data = await fetchWithAuth('/system/tools');
      return Array.isArray(data) ? data : [];
    } catch (error) {
      console.warn('Failed to fetch system tools from API', error);
      throw error;
    }
  },

  async getSandboxStatus(): Promise<SandboxStatusResponse> {
    try {
      return await fetchWithAuth('/system/sandbox');
    } catch (error) {
      console.warn('Failed to fetch sandbox status from API', error);
      throw error;
    }
  },
};

// --- Mock Data for Development ---
// (Mocks removed to prevent masking integration issues)
