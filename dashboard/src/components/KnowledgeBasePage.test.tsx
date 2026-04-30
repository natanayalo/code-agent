import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { KnowledgeBasePage } from './KnowledgeBasePage';
import { api } from '../services/api';

vi.mock('../services/api', () => ({
  api: {
    listPersonalMemory: vi.fn(),
    listProjectMemory: vi.fn(),
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

describe('KnowledgeBasePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('confirm', vi.fn(() => true));
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
    vi.mocked(api.listPersonalMemory).mockResolvedValue([
      {
        memory_id: 'pm-1',
        user_id: 'user-1',
        memory_key: 'communication_style',
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
        repo_url: 'https://github.com/natanayalo/code-agent',
        memory_key: 'build_command',
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

    renderKnowledgeBasePage();

    expect(await screen.findByRole('heading', { name: /Knowledge Base/i })).toBeInTheDocument();
    fireEvent.change(await screen.findByLabelText('Personal User ID'), { target: { value: 'user-1' } });
    expect(await screen.findByText('communication_style')).toBeInTheDocument();
    expect(await screen.findByText('build_command')).toBeInTheDocument();
  });

  it('submits personal memory upsert payload', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
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
  });

  it('shows validation error for non-object JSON values', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);

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
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockRejectedValue(new Error('project failed'));

    renderKnowledgeBasePage();

    expect(await screen.findByText('Error loading project memory')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry Project Memory' }));

    await waitFor(() => {
      expect(api.listProjectMemory).toHaveBeenCalledTimes(2);
    });
    expect(api.listPersonalMemory).not.toHaveBeenCalled();
  });

  it('submits project memory payload and handles empty optional values', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
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
  });

  it('validates project confidence range', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);

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
    vi.mocked(api.listProjectMemory).mockImplementation(async (_repoUrl, limit) =>
      Array.from({ length: limit ?? 0 }, (_, index) => ({
        memory_id: `pj-${limit}-${index}`,
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

    renderKnowledgeBasePage();

    expect(await screen.findByRole('button', { name: 'Load More Project Entries' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Load More Project Entries' }));

    await waitFor(() => {
      expect(api.listProjectMemory).toHaveBeenCalledWith(undefined, 100, 0);
    });
  });

  it('caps project pagination at backend limit', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockImplementation(async (_repoUrl, limit) =>
      Array.from({ length: limit ?? 0 }, (_, index) => ({
        memory_id: `pj-cap-${limit}-${index}`,
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

    renderKnowledgeBasePage();

    fireEvent.click(await screen.findByRole('button', { name: 'Load More Project Entries' })); // 50 -> 100
    await waitFor(() => expect(api.listProjectMemory).toHaveBeenCalledWith(undefined, 100, 0));

    fireEvent.click(await screen.findByRole('button', { name: 'Load More Project Entries' })); // 100 -> 150
    await waitFor(() => expect(api.listProjectMemory).toHaveBeenCalledWith(undefined, 150, 0));

    fireEvent.click(await screen.findByRole('button', { name: 'Load More Project Entries' })); // 150 -> 200
    await waitFor(() => expect(api.listProjectMemory).toHaveBeenCalledWith(undefined, 200, 0));

    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: 'Load More Project Entries' })
      ).not.toBeInTheDocument();
    });
  });
});
