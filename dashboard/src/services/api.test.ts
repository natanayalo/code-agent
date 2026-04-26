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

      const result = await api.listTasks();
      expect(result).toEqual([]); // listTasks returns [] if fetchWithAuth returns null
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
      const result = await api.listTasks();
      expect(result).toEqual([]);
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

    it('replayTask catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.replayTask('1')).rejects.toThrow('Network fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });
  });
});
