import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
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

    fireEvent.click(screen.getByRole('button', { name: 'Save Personal Entry' }));

    await waitFor(() => expect(api.upsertPersonalMemory).toHaveBeenCalledTimes(1));
    expect(api.upsertPersonalMemory).toHaveBeenCalledWith({
      user_id: 'user-2',
      memory_key: 'editor',
      value: { theme: 'light' },
      source: undefined,
      scope: undefined,
      confidence: 1,
      requires_verification: true,
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
    const deleteButtons = screen.getAllByTitle('Delete entry');
    fireEvent.click(deleteButtons[0]);

    await waitFor(() => {
      expect(api.deleteProjectMemory).toHaveBeenCalledWith(
        'https://github.com/natanayalo/code-agent',
        'remove_me'
      );
    });
  });
});
