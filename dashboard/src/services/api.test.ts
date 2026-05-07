import { describe, it, expect, vi, beforeEach } from 'vitest';
import { api } from './api';

// Mock fetch
const mockFetch = vi.fn();
global.fetch = mockFetch;

describe('api service', () => {
  beforeEach(() => {
    mockFetch.mockReset();
  });

  describe('fetchWithAuth', () => {
    it('sets credentials to include', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ success: true }),
      });

      await api.listTasks();

      const [, options] = mockFetch.mock.calls[0];
      expect(options.credentials).toBe('include');
      expect(options.headers['Content-Type']).toBe('application/json');
    });

    it('throws error on non-ok response', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 403,
        statusText: 'Forbidden',
        headers: new Map(),
      });

      await expect(api.listTasks()).rejects.toThrow('API Error: 403 Forbidden');
    });

    it('parses error detail from JSON response', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 400,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ detail: 'Custom error message' }),
      });

      await expect(api.listTasks()).rejects.toThrow('Custom error message');
    });

    it('returns null on non-json response', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'text/plain']]),
        text: async () => 'not json',
      });

      await expect(api.listTasks()).rejects.toThrow(
        'Expected JSON response but received content-type: text/plain'
      );
    });

    it('handles 204 No Content', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 204,
        headers: new Map(),
      });

      const result = await api.decideTaskApproval('1', true);
      expect(result).toBeNull();
    });
  });

  describe('auth methods', () => {
    it('login sends correct POST request', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ status: 'ok' }),
      });

      await api.auth.login('secret123');

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/auth/login');
      expect(options.method).toBe('POST');
      expect(JSON.parse(options.body)).toEqual({ secret: 'secret123' });
    });

    it('logout sends POST request', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ status: 'ok' }),
      });

      await api.auth.logout();

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/auth/logout');
      expect(options.method).toBe('POST');
    });

    it('status returns authenticated status', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ authenticated: true }),
      });

      const result = await api.auth.status();
      expect(result.authenticated).toBe(true);
    });
  });

  describe('api methods', () => {
    it('listTasks returns array of tasks', async () => {
      const mockTasks = [{ task_id: '1', task_text: 'test' }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockTasks,
      });

      const result = await api.listTasks();
      expect(result).toEqual(mockTasks);
    });

    it('getTask returns full task snapshot', async () => {
      const mockTask = { task_id: 'task-1', task_text: 'Inspect task', timeline: [] };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockTask,
      });

      const result = await api.getTask('task-1');
      const [url] = mockFetch.mock.calls[0];

      expect(url).toContain('/tasks/task-1');
      expect(result).toEqual(mockTask);
    });

    it('listTasks returns empty array if response is not an array', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ not: 'an array' }),
      });

      const result = await api.listTasks();
      expect(result).toEqual([]);
    });

    it('listSessions returns array of sessions', async () => {
      const mockSessions = [{ session_id: 's1' }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockSessions,
      });

      const result = await api.listSessions();
      expect(result).toEqual(mockSessions);
    });

    it('getTask catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.getTask('task-1')).rejects.toThrow('Network fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('decideTaskApproval sends correct POST body', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({}),
      });

      await api.decideTaskApproval('task-123', true);

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/tasks/task-123/approval');
      expect(options.method).toBe('POST');
      expect(JSON.parse(options.body)).toEqual({ approved: true });
    });

    it('listSessions returns empty array if response is not an array', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ not: 'an array' }),
      });

      const result = await api.listSessions();
      expect(result).toEqual([]);
    });

    it('listPersonalMemory returns array of entries', async () => {
      const mockEntries = [{ memory_id: 'm1', user_id: 'u1', memory_key: 'style', value: {} }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockEntries,
      });

      const result = await api.listPersonalMemory('u1');
      const [url] = mockFetch.mock.calls[0];

      expect(url).toContain('/knowledge-base/personal?user_id=u1');
      expect(result).toEqual(mockEntries);
    });

    it('listPersonalMemory includes pagination params and handles non-array fallback', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ not: 'array' }),
      });

      const result = await api.listPersonalMemory('u-2', 25, 50);
      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain('/knowledge-base/personal?');
      expect(url).toContain('user_id=u-2');
      expect(url).toContain('limit=25');
      expect(url).toContain('offset=50');
      expect(result).toEqual([]);
    });

    it('upsertPersonalMemory sends PUT payload', async () => {
      const mockEntry = {
        memory_id: 'm1',
        user_id: 'u1',
        memory_key: 'style',
        value: { style: 'concise' },
      };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockEntry,
      });

      await api.upsertPersonalMemory({
        user_id: 'u1',
        memory_key: 'style',
        value: { style: 'concise' },
      });

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/knowledge-base/personal');
      expect(options.method).toBe('PUT');
      expect(JSON.parse(options.body)).toEqual({
        user_id: 'u1',
        memory_key: 'style',
        value: { style: 'concise' },
      });
    });

    it('listPersonalMemory catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.listPersonalMemory('u1')).rejects.toThrow('Network fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('upsertPersonalMemory catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(
        api.upsertPersonalMemory({ user_id: 'u1', memory_key: 'k1', value: {} })
      ).rejects.toThrow('Network fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('deletePersonalMemory sends DELETE with encoded query params', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 204,
        headers: new Map(),
      });

      await api.deletePersonalMemory('u 1', 'key/one');

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/knowledge-base/personal?');
      expect(url).toContain('user_id=u+1');
      expect(url).toContain('memory_key=key%2Fone');
      expect(options.method).toBe('DELETE');
    });

    it('deletePersonalMemory catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.deletePersonalMemory('u1', 'k1')).rejects.toThrow('Network fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('listProjectMemory returns entries and supports no filter query', async () => {
      const mockEntries = [{ memory_id: 'p1', repo_url: 'https://repo', memory_key: 'k1', value: {} }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockEntries,
      });

      const result = await api.listProjectMemory();
      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain('/knowledge-base/project');
      expect(url).not.toContain('repo_url=');
      expect(result).toEqual(mockEntries);
    });

    it('listProjectMemory includes optional repo and pagination filters', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ([]),
      });

      await api.listProjectMemory('https://repo', 10, 20);
      const [url] = mockFetch.mock.calls[0];

      expect(url).toContain('/knowledge-base/project?');
      expect(url).toContain('repo_url=https%3A%2F%2Frepo');
      expect(url).toContain('limit=10');
      expect(url).toContain('offset=20');
    });

    it('listProjectMemory catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.listProjectMemory('https://repo')).rejects.toThrow('Network fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('upsertProjectMemory sends PUT payload', async () => {
      const mockEntry = {
        memory_id: 'p1',
        repo_url: 'https://repo',
        memory_key: 'k1',
        value: { cmd: 'pytest' },
      };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockEntry,
      });

      await api.upsertProjectMemory({
        repo_url: 'https://repo',
        memory_key: 'k1',
        value: { cmd: 'pytest' },
      });

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/knowledge-base/project');
      expect(options.method).toBe('PUT');
      expect(JSON.parse(options.body)).toEqual({
        repo_url: 'https://repo',
        memory_key: 'k1',
        value: { cmd: 'pytest' },
      });
    });

    it('upsertProjectMemory catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(
        api.upsertProjectMemory({ repo_url: 'https://repo', memory_key: 'k1', value: {} })
      ).rejects.toThrow('Network fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('deleteProjectMemory sends DELETE with encoded query params', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 204,
        headers: new Map(),
      });

      await api.deleteProjectMemory('https://github.com/natanayalo/code-agent', 'build command');

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/knowledge-base/project?');
      expect(url).toContain('memory_key=build+command');
      expect(options.method).toBe('DELETE');
    });

    it('deleteProjectMemory catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.deleteProjectMemory('https://repo', 'k1')).rejects.toThrow('Network fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('decideTaskApproval rejection sends correct POST body and logs correctly', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({}),
      });

      await api.decideTaskApproval('task-456', false);

      const [, options] = mockFetch.mock.calls[0];
      expect(JSON.parse(options.body)).toEqual({ approved: false });
    });

    it('parses non-string error detail from JSON response', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 400,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ detail: { msg: 'Complex error' } }),
      });

      await expect(api.listTasks()).rejects.toThrow('{"msg":"Complex error"}');
    });

    it('listTasks catch block rethrows error', async () => {
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.listTasks()).rejects.toThrow('Network fail');
    });

    it('listSessions catch block rethrows error', async () => {
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.listSessions()).rejects.toThrow('Network fail');
    });

    it('decideTaskApproval catch block rethrows error', async () => {
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.decideTaskApproval('1', true)).rejects.toThrow('Network fail');
    });

    it('throws on JSON parse failure', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => { throw new Error('Bad JSON'); },
      });

      await expect(api.listTasks()).rejects.toThrow('Failed to parse server response as JSON');
    });

    it('handles base URL with trailing slash from proxy', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ([]),
      });
      await api.listTasks();
      const [url] = mockFetch.mock.calls[0];
      // URL constructor with /api and relative path /tasks results in /tasks
      // But we use new URL(endpoint.replace(/^\//, ''), new URL(baseUrl, window.location.origin))
      expect(url).toContain('/tasks');
    });

    it('handles error response without detail field', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 400,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ unexpected: 'format' }),
      });
      await expect(api.listTasks()).rejects.toThrow('API Error: 400');
    });

    it('handles response with missing content-type header', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map(),
        json: async () => ([]),
      });
      await expect(api.listTasks()).rejects.toThrow(
        'Expected JSON response but received content-type: unknown'
      );
    });

    it('replayTask sends correct POST request', async () => {
      const mockSnapshot = { task_id: 'new-task', status: 'pending' };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 201,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockSnapshot,
      });

      const result = await api.replayTask('old-task');

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/tasks/old-task/replay');
      expect(options.method).toBe('POST');
      expect(result).toEqual(mockSnapshot);
    });

    it('replayTask sends override payload when provided', async () => {
      const mockSnapshot = { task_id: 'new-task', status: 'pending' };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 201,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockSnapshot,
      });

      await api.replayTask('old-task', {
        worker_override: 'gemini',
        constraints: { max_files: 3 },
        budget: { max_steps: 10 },
        secrets: { API_TOKEN: 'redacted' },
      });

      const [, options] = mockFetch.mock.calls[0];
      expect(options.method).toBe('POST');
      expect(JSON.parse(options.body)).toEqual({
        worker_override: 'gemini',
        constraints: { max_files: 3 },
        budget: { max_steps: 10 },
        secrets: { API_TOKEN: 'redacted' },
      });
    });

    it('replayTask catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.replayTask('1')).rejects.toThrow('Network fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('getMetrics sends correct GET request', async () => {
      const mockMetrics = { total_tasks: 10 };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockMetrics,
      });

      const result = await api.getMetrics(48);

      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain('/metrics?window_hours=48');
      expect(result).toEqual(mockMetrics);
    });

    it('getMetrics catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Metrics fail'));
      await expect(api.getMetrics()).rejects.toThrow('Metrics fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('respondToInteraction sends correct POST request', async () => {
      const mockSnapshot = { task_id: 'task-1', status: 'pending' };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockSnapshot,
      });

      const result = await api.respondToInteraction('task-1', 'int-1', 'resolved', { text: 'ok' });

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/tasks/task-1/interactions/int-1/response');
      expect(options.method).toBe('POST');
      expect(JSON.parse(options.body)).toEqual({
        status: 'resolved',
        response_data: { text: 'ok' },
      });
      expect(result).toEqual(mockSnapshot);
    });

    it('respondToInteraction catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.respondToInteraction('1', '2', 'resolved')).rejects.toThrow('Network fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('getSystemTools returns tools array', async () => {
      const mockTools = [{ name: 'test-tool' }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockTools,
      });

      const result = await api.getSystemTools();
      expect(result).toEqual(mockTools);
    });

    it('getSystemTools returns empty array if not an array', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ unexpected: 'data' }),
      });

      const result = await api.getSystemTools();
      expect(result).toEqual([]);
    });

    it('getSandboxStatus returns status', async () => {
      const mockStatus = { healthy: true };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockStatus,
      });

      const result = await api.getSandboxStatus();
      expect(result).toEqual(mockStatus);
    });
  });
});
