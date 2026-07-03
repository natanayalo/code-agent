import React from 'react';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { KnowledgeBasePage } from './KnowledgeBasePage';
import { api } from '../services/api';

vi.mock('../services/api', () => ({
  api: {
    listPersonalMemory: vi.fn(),
    searchPersonalMemory: vi.fn(),
    listProjectMemory: vi.fn(),
    searchProjectMemory: vi.fn(),
    upsertPersonalMemory: vi.fn(),
    upsertProjectMemory: vi.fn(),
    deletePersonalMemory: vi.fn(),
    deleteProjectMemory: vi.fn(),
  },
}));

function renderKnowledgeBasePage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <KnowledgeBasePage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function waitForDebounce() {
  await act(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 350));
  });
}

describe('KnowledgeBasePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('confirm', vi.fn(() => true));
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders loading state', () => {
    vi.mocked(api.listPersonalMemory).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.listProjectMemory).mockReturnValue(new Promise(() => {}));

    renderKnowledgeBasePage();

    expect(screen.getByText('Loading knowledge base...')).toBeInTheDocument();
  });

  it('renders personal and project memory entries', async () => {
    const longToken = `edge-${'x'.repeat(72)}`;
    const personalKey = `communication_style_${longToken}`;
    const projectKey = `build_command_${longToken}`;
    vi.mocked(api.listPersonalMemory).mockResolvedValue([
      {
        memory_id: 'pm-1',
        user_id: 'user-1',
        memory_key: personalKey,
        value: { style: 'concise' },
        confidence: 0.8,
        scope: 'global',
        source: 'operator',
        last_verified_at: null,
        requires_verification: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([
      {
        memory_id: 'pj-1',
        repo_url: `https://github.com/natanayalo/${longToken}`,
        memory_key: projectKey,
        value: { cmd: '.venv/bin/pytest' },
        confidence: 1.0,
        scope: 'repo',
        source: 'sandbox',
        last_verified_at: null,
        requires_verification: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    expect(await screen.findByRole('heading', { name: /Knowledge Base/i })).toBeInTheDocument();
    fireEvent.change(await screen.findByLabelText('Personal User ID'), { target: { value: 'user-1' } });
    expect(await screen.findByText(personalKey)).toBeInTheDocument();
    expect(await screen.findByText(projectKey)).toBeInTheDocument();
    expect(screen.getByText(projectKey).closest('.knowledge-entry-header')).toBeInTheDocument();
  });

  it('submits personal memory upsert payload', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.upsertPersonalMemory).mockResolvedValue({
      memory_id: 'pm-2',
      user_id: 'user-2',
      memory_key: 'editor',
      value: { theme: 'light' },
      confidence: 1.0,
      source: null,
      scope: null,
      last_verified_at: null,
      requires_verification: true,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });

    renderKnowledgeBasePage();

    await screen.findByLabelText('Personal User ID');

    fireEvent.change(screen.getByLabelText('Personal User ID'), { target: { value: 'user-2' } });
    fireEvent.change(screen.getByLabelText('Personal Memory Key'), { target: { value: 'editor' } });
    fireEvent.change(screen.getByLabelText('Personal Memory Value (JSON object)'), {
      target: { value: '{"theme":"light"}' },
    });
    fireEvent.change(screen.getByLabelText('Personal Source'), { target: { value: 'operator' } });
    fireEvent.change(screen.getByLabelText('Personal Scope'), { target: { value: 'global' } });
    fireEvent.change(screen.getByLabelText('Personal Confidence (0.0-1.0)'), {
      target: { value: '0.25' },
    });
    fireEvent.click(screen.getAllByLabelText('Requires verification')[0]);

    fireEvent.click(screen.getByRole('button', { name: 'Save Personal Entry' }));

    await waitFor(() => expect(api.upsertPersonalMemory).toHaveBeenCalledTimes(1));
    expect(api.upsertPersonalMemory).toHaveBeenCalledWith({
      user_id: 'user-2',
      memory_key: 'editor',
      value: { theme: 'light' },
      source: 'operator',
      scope: 'global',
      confidence: 0.25,
      requires_verification: false,
    });
    expect(screen.getByLabelText('Personal User ID')).toHaveValue('user-2');
    expect(screen.getByLabelText('Personal Memory Key')).toHaveValue('');
    expect(screen.getByLabelText('Personal Memory Value (JSON object)')).toHaveValue('{\n  \n}');
    expect(screen.getByLabelText('Personal Source')).toHaveValue('');
    expect(screen.getByLabelText('Personal Scope')).toHaveValue('');
    expect(Number((screen.getByLabelText('Personal Confidence (0.0-1.0)') as HTMLInputElement).value)).toBe(
      1
    );
    expect(screen.getAllByLabelText('Requires verification')[0]).toBeChecked();
  });

  it('shows validation error for non-object JSON values', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    await screen.findByLabelText('Personal User ID');
    fireEvent.change(screen.getByLabelText('Personal User ID'), { target: { value: 'user-3' } });
    fireEvent.change(screen.getByLabelText('Personal Memory Key'), { target: { value: 'bad-json' } });
    fireEvent.change(screen.getByLabelText('Personal Memory Value (JSON object)'), {
      target: { value: '"not an object"' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Personal Entry' }));

    expect(
      await screen.findByText('Memory value must be a JSON object.')
    ).toBeInTheDocument();
    expect(api.upsertPersonalMemory).not.toHaveBeenCalled();
  });

  it('deletes a project memory entry', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([
      {
        memory_id: 'pj-delete',
        repo_url: 'https://github.com/natanayalo/code-agent',
        memory_key: 'remove_me',
        value: { stale: true },
        confidence: 0.5,
        scope: 'repo',
        source: null,
        last_verified_at: null,
        requires_verification: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.deleteProjectMemory).mockResolvedValue();

    renderKnowledgeBasePage();

    await screen.findByText('remove_me');
    fireEvent.click(screen.getByLabelText('Delete project memory remove_me'));

    await waitFor(() => {
      expect(api.deleteProjectMemory).toHaveBeenCalledWith(
        'https://github.com/natanayalo/code-agent',
        'remove_me'
      );
    });
  });

  it('shows project-section load error and retries project query', async () => {
    const longError = `project failed ${'x'.repeat(96)}`;
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockRejectedValue(new Error(longError));
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    expect(await screen.findByText('Error loading project memory')).toBeInTheDocument();
    expect(screen.getByText(longError).closest('.error-container')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry Project Memory' }));

    await waitFor(() => {
      expect(api.listProjectMemory).toHaveBeenCalledTimes(2);
    });
    expect(api.listPersonalMemory).not.toHaveBeenCalled();
  });

  it('submits project memory payload and handles empty optional values', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.upsertProjectMemory).mockResolvedValue({
      memory_id: 'pj-upsert',
      repo_url: 'https://example.com/repo',
      memory_key: 'build',
      value: { cmd: 'npm test' },
      confidence: 1.0,
      source: null,
      scope: null,
      last_verified_at: new Date().toISOString(),
      requires_verification: false,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });

    renderKnowledgeBasePage();

    await screen.findByLabelText('Project Repository URL');
    fireEvent.change(screen.getByLabelText('Project Repository URL'), {
      target: { value: '  https://example.com/repo  ' },
    });
    fireEvent.change(screen.getByLabelText('Project Memory Key'), {
      target: { value: '  build  ' },
    });
    fireEvent.change(screen.getByLabelText('Project Memory Value (JSON object)'), {
      target: { value: '{"cmd":"npm test"}' },
    });
    fireEvent.change(screen.getByLabelText('Project Source'), {
      target: { value: '   ' },
    });
    fireEvent.change(screen.getByLabelText('Project Scope'), {
      target: { value: '   ' },
    });
    fireEvent.click(screen.getAllByLabelText('Requires verification')[1]);

    fireEvent.click(screen.getByRole('button', { name: 'Save Project Entry' }));

    await waitFor(() => expect(api.upsertProjectMemory).toHaveBeenCalledTimes(1));
    expect(api.upsertProjectMemory).toHaveBeenCalledWith({
      repo_url: 'https://example.com/repo',
      memory_key: 'build',
      value: { cmd: 'npm test' },
      source: undefined,
      scope: undefined,
      confidence: 1,
      requires_verification: false,
    });
    expect(screen.getByLabelText('Project Repository URL')).toHaveValue('  https://example.com/repo  ');
    expect(screen.getByLabelText('Project Memory Key')).toHaveValue('');
    expect(screen.getByLabelText('Project Memory Value (JSON object)')).toHaveValue('{\n  \n}');
    expect(screen.getByLabelText('Project Source')).toHaveValue('');
    expect(screen.getByLabelText('Project Scope')).toHaveValue('');
    expect(Number((screen.getByLabelText('Project Confidence (0.0-1.0)') as HTMLInputElement).value)).toBe(
      1
    );
    expect(screen.getAllByLabelText('Requires verification')[1]).toBeChecked();
  });

  it('validates project confidence range', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    await screen.findByLabelText('Project Repository URL');
    fireEvent.change(screen.getByLabelText('Project Repository URL'), {
      target: { value: 'https://example.com/repo' },
    });
    fireEvent.change(screen.getByLabelText('Project Memory Key'), {
      target: { value: 'build' },
    });
    fireEvent.change(screen.getByLabelText('Project Memory Value (JSON object)'), {
      target: { value: '{"cmd":"npm test"}' },
    });
    const projectConfidenceInput = screen.getByLabelText(
      'Project Confidence (0.0-1.0)'
    ) as HTMLInputElement;
    fireEvent.change(projectConfidenceInput, {
      target: { value: '1.5' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Project Entry' }));

    expect(projectConfidenceInput.validity.rangeOverflow).toBe(true);
    expect(api.upsertProjectMemory).not.toHaveBeenCalled();
  });

  it('uses numeric inputs for confidence fields', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    await screen.findByLabelText('Personal Confidence (0.0-1.0)');
    expect(screen.getByLabelText('Personal Confidence (0.0-1.0)')).toHaveAttribute('type', 'number');
    expect(screen.getByLabelText('Project Confidence (0.0-1.0)')).toHaveAttribute('type', 'number');
  });

  it('deletes a personal memory entry', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([
      {
        memory_id: 'pm-delete',
        user_id: 'user-delete',
        memory_key: 'remove_personal',
        value: { stale: true },
        confidence: 0.4,
        scope: 'global',
        source: null,
        last_verified_at: new Date().toISOString(),
        requires_verification: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.deletePersonalMemory).mockResolvedValue();

    renderKnowledgeBasePage();

    fireEvent.change(await screen.findByLabelText('Personal User ID'), {
      target: { value: 'user-delete' },
    });
    await screen.findByText('remove_personal');
    fireEvent.click(screen.getByLabelText('Delete personal memory remove_personal'));

    await waitFor(() => {
      expect(api.deletePersonalMemory).toHaveBeenCalledWith('user-delete', 'remove_personal');
    });
  });

  it('shows delete errors for project entries', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([
      {
        memory_id: 'pj-error',
        repo_url: 'https://github.com/natanayalo/code-agent',
        memory_key: 'cannot-delete',
        value: { stale: true },
        confidence: 0.5,
        scope: 'repo',
        source: null,
        last_verified_at: null,
        requires_verification: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.deleteProjectMemory).mockRejectedValue(new Error('delete failed'));

    renderKnowledgeBasePage();

    await screen.findByText('cannot-delete');
    fireEvent.click(screen.getByLabelText('Delete project memory cannot-delete'));

    expect(await screen.findByText('delete failed')).toBeInTheDocument();
  });

  it('does not delete when confirmation is cancelled', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([
      {
        memory_id: 'pj-confirm',
        repo_url: 'https://github.com/natanayalo/code-agent',
        memory_key: 'confirm-me',
        value: { stale: true },
        confidence: 0.5,
        scope: 'repo',
        source: null,
        last_verified_at: null,
        requires_verification: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.deleteProjectMemory).mockResolvedValue();
    vi.stubGlobal('confirm', vi.fn(() => false));

    renderKnowledgeBasePage();

    await screen.findByText('confirm-me');
    fireEvent.click(screen.getByLabelText('Delete project memory confirm-me'));

    expect(api.deleteProjectMemory).not.toHaveBeenCalled();
  });

  it('requires a personal user id before loading personal entries', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    expect(
      await screen.findByText('Enter a personal user ID above to load entries.')
    ).toBeInTheDocument();
    expect(api.listPersonalMemory).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Personal User ID'), { target: { value: 'user-filter' } });
    expect(api.listPersonalMemory).not.toHaveBeenCalled();

    await waitFor(() => {
      expect(api.listPersonalMemory).toHaveBeenCalledWith('user-filter', 50, 0);
    });
  });

  it('loads more project entries with pagination controls', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockImplementation(async (_repoUrl, limit, offset) =>
      Array.from({ length: limit ?? 0 }, (_, index) => ({
        memory_id: `pj-${offset}-${index}`,
        repo_url: 'https://github.com/natanayalo/code-agent',
        memory_key: `entry-${index}`,
        value: { index },
        confidence: 1,
        scope: 'repo',
        source: null,
        last_verified_at: null,
        requires_verification: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }))
    );
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    expect(await screen.findByRole('button', { name: 'Load More Project Entries' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Load More Project Entries' }));

    await waitFor(() => {
      expect(api.listProjectMemory).toHaveBeenCalledWith(undefined, 50, 50);
    });
  });

  it('loads more personal entries with pagination controls', async () => {
    vi.mocked(api.listPersonalMemory).mockImplementation(async (_userId, limit, offset) =>
      Array.from({ length: limit ?? 0 }, (_, index) => ({
        memory_id: `pm-${offset}-${index}`,
        user_id: 'user-load-more',
        memory_key: `entry-${index}`,
        value: { index },
        confidence: 1,
        scope: 'global',
        source: null,
        last_verified_at: null,
        requires_verification: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }))
    );
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    fireEvent.change(await screen.findByLabelText('Personal User ID'), {
      target: { value: 'user-load-more' },
    });
    await waitFor(() => {
      expect(api.listPersonalMemory).toHaveBeenCalledWith('user-load-more', 50, 0);
    });
    expect(await screen.findByText('entry-0')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Load More Personal Entries' }));

    await waitFor(() => {
      expect(api.listPersonalMemory).toHaveBeenCalledWith('user-load-more', 50, 50);
    });
  });

  it('caps project pagination at backend limit', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockImplementation(async (_repoUrl, limit, offset) =>
      Array.from({ length: limit ?? 0 }, (_, index) => ({
        memory_id: `pj-cap-${offset}-${index}`,
        repo_url: 'https://github.com/natanayalo/code-agent',
        memory_key: `cap-${index}`,
        value: { index },
        confidence: 1,
        scope: 'repo',
        source: null,
        last_verified_at: null,
        requires_verification: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }))
    );
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    fireEvent.click(await screen.findByRole('button', { name: 'Load More Project Entries' })); // 0 -> 50
    await waitFor(() => expect(api.listProjectMemory).toHaveBeenCalledWith(undefined, 50, 50));

    fireEvent.click(await screen.findByRole('button', { name: 'Load More Project Entries' })); // 50 -> 100
    await waitFor(() => expect(api.listProjectMemory).toHaveBeenCalledWith(undefined, 50, 100));

    fireEvent.click(await screen.findByRole('button', { name: 'Load More Project Entries' })); // 100 -> 150
    await waitFor(() => expect(api.listProjectMemory).toHaveBeenCalledWith(undefined, 50, 150));

    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: 'Load More Project Entries' })
      ).not.toBeInTheDocument();
    });
  });

  it('searches memory with debounce and safely renders highlighted snippets', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([
      {
        memory_id: 'pm-search',
        user_id: 'user-search',
        memory_key: 'preferred_test',
        value: { cmd: '.venv/bin/pytest' },
        headline: 'Run __CA_MARK_START__pytest__CA_MARK_END__ first <script>alert(1)</script>',
        confidence: 1,
        scope: 'global',
        source: 'operator',
        last_verified_at: null,
        requires_verification: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([
      {
        memory_id: 'pj-search',
        repo_url: 'https://github.com/natanayalo/code-agent',
        memory_key: 'build_memory',
        value: { cmd: 'npm run test:run' },
        headline: 'Check __CA_MARK_START__test__CA_MARK_END__ coverage',
        confidence: 0.8,
        scope: 'repo',
        source: 'operator',
        last_verified_at: null,
        requires_verification: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);

    renderKnowledgeBasePage();

    fireEvent.change(await screen.findByLabelText('Personal User ID'), {
      target: { value: 'user-search' },
    });
    fireEvent.change(screen.getByLabelText('Project Repository URL'), {
      target: { value: 'https://github.com/natanayalo/code-agent' },
    });
    fireEvent.change(screen.getByLabelText('Search Query'), {
      target: { value: 'pytest' },
    });

    await waitForDebounce();
    await waitFor(() => {
      expect(api.searchPersonalMemory).toHaveBeenCalledWith('user-search', 'pytest', 20);
    });
    await waitFor(() => {
      expect(api.searchProjectMemory).toHaveBeenCalledWith(
        'https://github.com/natanayalo/code-agent',
        'pytest',
        20
      );
    });

    const mark = await screen.findByText('pytest', { selector: 'mark' });
    expect(mark.tagName).toBe('MARK');
    expect(screen.getByText((content) => content.includes('<script>alert(1)</script>'))).toBeInTheDocument();
    expect(screen.getByText('preferred_test')).toBeInTheDocument();
    expect(screen.getByText('build_memory')).toBeInTheDocument();
    expect(screen.getByText(/Searching for/i)).toHaveTextContent('1 personal');
    expect(screen.getByText(/Searching for/i)).toHaveTextContent('1 project');
  });

  it('shows the short-query helper without triggering search requests', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    fireEvent.change(screen.getByLabelText('Search Query'), {
      target: { value: 'p' },
    });
    await waitForDebounce();

    expect(screen.getByText('Type at least 2 characters to start searching.')).toBeInTheDocument();
    expect(api.searchPersonalMemory).not.toHaveBeenCalled();
    expect(api.searchProjectMemory).not.toHaveBeenCalled();
  });

  it('retries failed project search requests and renders malformed headlines safely', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory)
      .mockRejectedValueOnce(new Error('search failed'))
      .mockResolvedValueOnce([
        {
          memory_id: 'pj-malformed',
          repo_url: 'https://github.com/natanayalo/code-agent',
          memory_key: 'broken_headline',
          value: { cmd: 'npm run test:run' },
          headline: '__CA_MARK_START__broken highlight',
          confidence: 0.8,
          scope: 'repo',
          source: 'operator',
          last_verified_at: null,
          requires_verification: true,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
      ]);

    renderKnowledgeBasePage();

    fireEvent.change(await screen.findByLabelText('Project Repository URL'), {
      target: { value: 'https://github.com/natanayalo/code-agent' },
    });
    fireEvent.change(screen.getByLabelText('Search Query'), {
      target: { value: 'tests' },
    });
    await waitForDebounce();

    expect(await screen.findByText('Error loading project search')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry Project Search' }));

    await waitFor(() => {
      expect(api.searchProjectMemory).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText('__CA_MARK_START__broken highlight')).toBeInTheDocument();
  });

  it('refreshes personal search results after saving while search mode is active', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.upsertPersonalMemory).mockResolvedValue({
      memory_id: 'pm-search-save',
      user_id: 'user-search-save',
      memory_key: 'editor',
      value: { theme: 'dark' },
      confidence: 1,
      source: null,
      scope: null,
      last_verified_at: null,
      requires_verification: true,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });

    renderKnowledgeBasePage();

    fireEvent.change(await screen.findByLabelText('Personal User ID'), {
      target: { value: 'user-search-save' },
    });
    fireEvent.change(screen.getByLabelText('Search Query'), {
      target: { value: 'ed' },
    });
    await waitForDebounce();
    await waitFor(() => {
      expect(api.searchPersonalMemory).toHaveBeenCalledWith('user-search-save', 'ed', 20);
    });

    fireEvent.change(screen.getByLabelText('Personal Memory Key'), {
      target: { value: 'editor' },
    });
    fireEvent.change(screen.getByLabelText('Personal Memory Value (JSON object)'), {
      target: { value: '{"theme":"dark"}' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Personal Entry' }));

    await waitFor(() => expect(api.upsertPersonalMemory).toHaveBeenCalledTimes(1));
    await waitFor(() => {
      expect(api.searchPersonalMemory).toHaveBeenCalledTimes(2);
    });
  });

  it('deletes project search results and refreshes the search query', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([
      {
        memory_id: 'pj-search-delete',
        repo_url: 'https://github.com/natanayalo/code-agent',
        memory_key: 'delete_search_result',
        value: { stale: true },
        confidence: 0.4,
        scope: 'repo',
        source: null,
        last_verified_at: null,
        requires_verification: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    vi.mocked(api.deleteProjectMemory).mockResolvedValue();

    renderKnowledgeBasePage();

    fireEvent.change(await screen.findByLabelText('Project Repository URL'), {
      target: { value: 'https://github.com/natanayalo/code-agent' },
    });
    fireEvent.change(screen.getByLabelText('Search Query'), {
      target: { value: 'de' },
    });
    await waitForDebounce();

    await screen.findByText('delete_search_result');
    fireEvent.click(screen.getByLabelText('Delete project memory delete_search_result'));

    await waitFor(() => {
      expect(api.deleteProjectMemory).toHaveBeenCalledWith(
        'https://github.com/natanayalo/code-agent',
        'delete_search_result'
      );
    });
    await waitFor(() => {
      expect(api.searchProjectMemory).toHaveBeenCalledTimes(2);
    });
  });

  it('clears search back to browse mode', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    fireEvent.change(await screen.findByLabelText('Personal User ID'), {
      target: { value: 'user-browse' },
    });
    await waitForDebounce();
    await waitFor(() => {
      expect(api.listPersonalMemory).toHaveBeenCalledWith('user-browse', 50, 0);
    });

    fireEvent.change(screen.getByLabelText('Search Query'), {
      target: { value: 'py' },
    });
    await waitForDebounce();
    await waitFor(() => {
      expect(api.searchPersonalMemory).toHaveBeenCalledWith('user-browse', 'py', 20);
    });

    fireEvent.click(screen.getByRole('button', { name: 'Clear Search' }));
    await waitForDebounce();

    expect(
      screen.getByText('Clear the query at any time to return to paginated browse mode.')
    ).toBeInTheDocument();
    expect(screen.getByText('No personal entries found.')).toBeInTheDocument();
  });
});
