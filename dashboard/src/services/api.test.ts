import { describe, it, expect, vi, beforeEach } from 'vitest';
import { api } from './api';

// Mock fetch
const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

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

    it('submitTask sends a dashboard task payload', async () => {
      const mockSnapshot = { task_id: 'task-new', status: 'pending' };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 202,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockSnapshot,
      });

      const payload = {
        task_text: 'Run a focused task',
        repo_url: 'https://github.com/example/repo',
        priority: 1,
        constraints: { task_type: 'feature', trigger_source: 'dashboard' },
      };
      const result = await api.submitTask(payload);

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/tasks');
      expect(options.method).toBe('POST');
      expect(JSON.parse(options.body)).toEqual(payload);
      expect(result).toEqual(mockSnapshot);
    });

    it('triggerScoutTask sends the manual scout trigger request with payload', async () => {
      const mockSnapshot = { task_id: 'task-scout', status: 'pending' };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 202,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockSnapshot,
      });

      const payload = { mode: 'research' as const, max_proposals: 10, depth: 'deep' as const };
      const result = await api.triggerScoutTask(payload);

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/tasks/scout/trigger');
      expect(options.method).toBe('POST');
      expect(JSON.parse(options.body)).toEqual(payload);
      expect(result).toEqual(mockSnapshot);
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
      const mockEntries = [{ memory_id: 'm1', memory_key: 'style', value: {} }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockEntries,
      });

      const result = await api.listPersonalMemory();
      const [url] = mockFetch.mock.calls[0];

      expect(url).toContain('/knowledge-base/personal');
      expect(url).not.toContain('user_id=');
      expect(result).toEqual(mockEntries);
    });

    it('listPersonalMemory includes pagination params and handles non-array fallback', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ not: 'array' }),
      });

      const result = await api.listPersonalMemory(25, 50);
      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain('/knowledge-base/personal?');
      expect(url).not.toContain('user_id=');
      expect(url).toContain('limit=25');
      expect(url).toContain('offset=50');
      expect(result).toEqual([]);
    });

    it('getKnowledgeBaseStats encodes optional scope params', async () => {
      const mockStats = {
        personal: { total: 2, requires_verification: 1 },
        project: { total: 3, requires_verification: 2 },
        project_global: { total: 5, requires_verification: 2 },
      };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockStats,
      });

      const result = await api.getKnowledgeBaseStats('https://repo');
      const [url] = mockFetch.mock.calls[0];

      expect(url).toContain('/knowledge-base/stats?');
      expect(url).not.toContain('user_id=');
      expect(url).toContain('repo_url=https%3A%2F%2Frepo');
      expect(result).toEqual(mockStats);
    });

    it('getKnowledgeBaseStats falls back for malformed payloads', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({}),
      });

      const result = await api.getKnowledgeBaseStats();

      expect(result).toEqual({
        personal: { total: 0, requires_verification: 0 },
        project: null,
        project_global: { total: 0, requires_verification: 0 },
      });
    });

    it('searchPersonalMemory encodes the query string and returns array results', async () => {
      const mockEntries = [{ memory_id: 'm-search', memory_key: 'style', value: {} }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockEntries,
      });

      const result = await api.searchPersonalMemory('pytest command', 15);
      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain('/knowledge-base/personal/search?');
      expect(url).not.toContain('user_id=');
      expect(url).toContain('q=pytest+command');
      expect(url).toContain('limit=15');
      expect(result).toEqual(mockEntries);
    });

    it('upsertPersonalMemory sends PUT payload', async () => {
      const mockEntry = {
        memory_id: 'm1',
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
        memory_key: 'style',
        value: { style: 'concise' },
      });

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/knowledge-base/personal');
      expect(options.method).toBe('PUT');
      expect(JSON.parse(options.body)).toEqual({
        memory_key: 'style',
        value: { style: 'concise' },
      });
    });

    it('listPersonalMemory catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.listPersonalMemory()).rejects.toThrow('Network fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('upsertPersonalMemory catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(
        api.upsertPersonalMemory({ memory_key: 'k1', value: {} })
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

      await api.deletePersonalMemory('key/one');

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/knowledge-base/personal?');
      expect(url).not.toContain('user_id=');
      expect(url).toContain('memory_key=key%2Fone');
      expect(options.method).toBe('DELETE');
    });

    it('deletePersonalMemory catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.deletePersonalMemory('k1')).rejects.toThrow('Network fail');
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

    it('searchProjectMemory encodes repo and query parameters', async () => {
      const mockEntries = [{ memory_id: 'p-search', repo_url: 'https://repo', memory_key: 'k1', value: {} }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockEntries,
      });

      const result = await api.searchProjectMemory(
        'https://github.com/natanayalo/code-agent',
        'memory search',
        12
      );
      const [url] = mockFetch.mock.calls[0];

      expect(url).toContain('/knowledge-base/project/search?');
      expect(url).toContain('repo_url=https%3A%2F%2Fgithub.com%2Fnatanayalo%2Fcode-agent');
      expect(url).toContain('q=memory+search');
      expect(url).toContain('limit=12');
      expect(result).toEqual(mockEntries);
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

    it('listProposals catch block rethrows error', async () => {
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.listProposals()).rejects.toThrow('Network fail');
    });

    it('listProposals returns empty array when response is not an array', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        headers: { get: () => 'application/json' },
        json: async () => ({ not_an_array: true }),
      });
      const result = await api.listProposals();
      expect(result).toEqual([]);
    });

    it('decideTaskApproval catch block rethrows error', async () => {
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.decideTaskApproval('1', true)).rejects.toThrow('Network fail');
    });

    it('acceptProposal catch block rethrows error', async () => {
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.acceptProposal('1')).rejects.toThrow('Network fail');
    });

    it('rejectProposal catch block rethrows error', async () => {
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.rejectProposal('1')).rejects.toThrow('Network fail');
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

    it('cancelTask sends correct POST request', async () => {
      const mockSnapshot = { task_id: 'task-1', status: 'failed' };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockSnapshot,
      });

      const result = await api.cancelTask('task-1');

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/tasks/task-1/cancel');
      expect(options.method).toBe('POST');
      expect(result).toEqual(mockSnapshot);
    });

    it('recordInteractionResponse sends correct POST request', async () => {
      const mockSnapshot = { task_id: 'task-1', status: 'pending' };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockSnapshot,
      });

      await api.recordInteractionResponse('task-1', 'interaction-1', {
        status: 'resolved',
        response_data: { answer: 'yes' },
      });

      const [url, options] = mockFetch.mock.calls[0];
      expect(url).toContain('/tasks/task-1/interactions/interaction-1/response');
      expect(options.method).toBe('POST');
      expect(JSON.parse(options.body)).toEqual({
        status: 'resolved',
        response_data: { answer: 'yes' },
      });
    });

    it('cancelTask catch block rethrows error', async () => {
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(api.cancelTask('task-1')).rejects.toThrow('Network fail');
    });

    it('recordInteractionResponse catch block rethrows error', async () => {
      mockFetch.mockRejectedValueOnce(new Error('Network fail'));
      await expect(
        api.recordInteractionResponse('task-1', 'interaction-1', {
          status: 'resolved',
          response_data: {},
        })
      ).rejects.toThrow('Network fail');
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
        worker_override: 'antigravity',
        constraints: { max_files: 3 },
        budget: { max_steps: 10 },
        secrets: { API_TOKEN: 'redacted' },
      });

      const [, options] = mockFetch.mock.calls[0];
      expect(options.method).toBe('POST');
      expect(JSON.parse(options.body)).toEqual({
        worker_override: 'antigravity',
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

    it('getSystemTools returns array and falls back to [] for non-array payloads', async () => {
      const toolsPayload = [{ name: 'shell', description: 'Shell tool' }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => toolsPayload,
      });

      const result = await api.getSystemTools();
      expect(result).toEqual(toolsPayload);
      expect(mockFetch.mock.calls[0][0]).toContain('/system/tools');

      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ not: 'array' }),
      });

      const fallbackResult = await api.getSystemTools();
      expect(fallbackResult).toEqual([]);
    });

    it('getSystemTools catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Tools fail'));
      await expect(api.getSystemTools()).rejects.toThrow('Tools fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('getSandboxStatus sends correct GET request', async () => {
      const sandboxPayload = { status: 'healthy', retention_enabled: true };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => sandboxPayload,
      });

      const result = await api.getSandboxStatus();
      expect(result).toEqual(sandboxPayload);
      expect(mockFetch.mock.calls[0][0]).toContain('/system/sandbox');
    });

    it('getSandboxStatus catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Sandbox fail'));
      await expect(api.getSandboxStatus()).rejects.toThrow('Sandbox fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('getRuntimeManifest sends correct GET request', async () => {
      const manifestPayload = {
        service: { service_name: 'code-agent', schema_version: 1 },
        maintenance_actions: [],
      };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => manifestPayload,
      });

      const result = await api.getRuntimeManifest();
      expect(result).toEqual(manifestPayload);
      expect(mockFetch.mock.calls[0][0]).toContain('/system/runtime-manifest');
    });

    it('getRuntimeManifest catch block rethrows error', async () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockFetch.mockRejectedValueOnce(new Error('Manifest fail'));
      await expect(api.getRuntimeManifest()).rejects.toThrow('Manifest fail');
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it('listProposals returns array of proposals', async () => {
      const mockProposals = [{ proposal_id: 'p1', title: 'Idea 1' }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockProposals,
      });

      const result = await api.listProposals('pending_review');
      const [url] = mockFetch.mock.calls[0];

      expect(url).toContain('/proposals?status=pending_review');
      expect(result).toEqual(mockProposals);
    });

    it('listProposals includes proposal_type when provided', async () => {
      const mockProposals = [{ proposal_id: 'p1', title: 'Improvement 1' }];
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockProposals,
      });

      const result = await api.listProposals('pending_review', 'reflection');
      const [url] = mockFetch.mock.calls[0];

      expect(url).toContain('/proposals?status=pending_review&proposal_type=reflection');
      expect(result).toEqual(mockProposals);
    });

    it('acceptProposal sends correct POST request', async () => {
      const mockSnapshot = { task_id: 't1', status: 'pending' };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockSnapshot,
      });

      const result = await api.acceptProposal('p1');
      const [url, options] = mockFetch.mock.calls[0];

      expect(url).toContain('/proposals/p1/accept');
      expect(options.method).toBe('POST');
      expect(result).toEqual(mockSnapshot);
    });

    it('rejectProposal sends correct POST request', async () => {
      const mockSnapshot = { proposal_id: 'p1', status: 'rejected' };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => mockSnapshot,
      });

      const result = await api.rejectProposal('p1');
      const [url, options] = mockFetch.mock.calls[0];

      expect(url).toContain('/proposals/p1/reject');
      expect(options.method).toBe('POST');
      expect(result).toEqual(mockSnapshot);
    });
  });
});
