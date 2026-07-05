import React from 'react';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { KnowledgeBasePage } from './KnowledgeBasePage';
import { api } from '../services/api';

vi.mock('../services/api', () => ({
  api: {
    getKnowledgeBaseStats: vi.fn(),
    listMemoryAdmissionDecisions: vi.fn(),
    listMemoryObservations: vi.fn(),
    listPersonalMemory: vi.fn(),
    searchPersonalMemory: vi.fn(),
    listProjectMemory: vi.fn(),
    searchProjectMemory: vi.fn(),
    listMemoryProposals: vi.fn(),
    createMemoryProposal: vi.fn(),
    acceptMemoryProposal: vi.fn(),
    rejectMemoryProposal: vi.fn(),
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

function openAddMemoryTab() {
  fireEvent.click(screen.getByRole('tab', { name: /Add Memory/i }));
}

describe('KnowledgeBasePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('confirm', vi.fn(() => true));
    vi.useRealTimers();
    vi.mocked(api.getKnowledgeBaseStats).mockResolvedValue({
      personal: { total: 0, requires_verification: 0 },
      project: null,
      project_global: { total: 0, requires_verification: 0 },
    });
    vi.mocked(api.listMemoryObservations).mockResolvedValue([]);
    vi.mocked(api.listMemoryAdmissionDecisions).mockResolvedValue([]);
    vi.mocked(api.listMemoryProposals).mockResolvedValue([]);
    vi.mocked(api.createMemoryProposal).mockResolvedValue({
      proposal_id: 'mp-created',
      category: 'project',
      repo_url: 'https://github.com/natanayalo/code-agent',
      memory_key: 'created',
      value: {},
      status: 'pending_review',
      confidence: 0.9,
      requires_verification: false,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    vi.mocked(api.acceptMemoryProposal).mockResolvedValue({
      proposal_id: 'mp-accepted',
      category: 'project',
      repo_url: 'https://github.com/natanayalo/code-agent',
      memory_key: 'accepted',
      value: {},
      status: 'accepted',
      confidence: 0.9,
      requires_verification: false,
      accepted_memory_id: 'memory-1',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    vi.mocked(api.rejectMemoryProposal).mockResolvedValue({
      proposal_id: 'mp-rejected',
      category: 'personal',
      memory_key: 'rejected',
      value: {},
      status: 'rejected',
      confidence: 0.9,
      requires_verification: true,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders loading state', () => {
    vi.mocked(api.getKnowledgeBaseStats).mockResolvedValue({
      personal: { total: 0, requires_verification: 0 },
      project: null,
      project_global: { total: 0, requires_verification: 0 },
    });
    vi.mocked(api.listPersonalMemory).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.listProjectMemory).mockReturnValue(new Promise(() => {}));

    renderKnowledgeBasePage();

    expect(screen.getByText('Loading knowledge base...')).toBeInTheDocument();
  });

  it('defaults to browse mode and shows inventory metrics', async () => {
    vi.mocked(api.getKnowledgeBaseStats).mockResolvedValue({
      personal: { total: 2, requires_verification: 1 },
      project: { total: 3, requires_verification: 2 },
      project_global: { total: 5, requires_verification: 2 },
    });
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    expect(await screen.findByRole('tab', { name: /Browse/i })).toHaveAttribute(
      'aria-selected',
      'true'
    );
    fireEvent.change(screen.getByLabelText('Project Repository URL'), {
      target: { value: 'https://github.com/natanayalo/code-agent' },
    });

    await waitForDebounce();
    await waitFor(() => {
      expect(api.getKnowledgeBaseStats).toHaveBeenCalledWith(
        'https://github.com/natanayalo/code-agent'
      );
    });
    await waitFor(() => {
      expect(api.listProjectMemory).toHaveBeenCalledWith(
        'https://github.com/natanayalo/code-agent',
        50,
        0
      );
    });
    await waitFor(() => {
      expect(screen.getAllByText('Personal Memory').length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getByText('2 memories')).toBeInTheDocument();
    expect(screen.getByText('All project memory')).toBeInTheDocument();
    expect(screen.getByText('5 memories')).toBeInTheDocument();
  });

  it('renders personal and project memory entries', async () => {
    const longToken = `edge-${'x'.repeat(72)}`;
    const personalKey = `communication_style_${longToken}`;
    const projectKey = `build_command_${longToken}`;
    vi.mocked(api.listPersonalMemory).mockResolvedValue([
      {
        memory_id: 'pm-1',
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
    expect(await screen.findByText(personalKey)).toBeInTheDocument();
    expect(await screen.findByText(projectKey)).toBeInTheDocument();
    expect(screen.getByText(projectKey).closest('.knowledge-entry-header')).toBeInTheDocument();
  });

  it('renders trace tab observation and decision lineage', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.listMemoryObservations).mockResolvedValue([
      {
        observation_id: 'obs-1',
        repo_url: 'https://github.com/natanayalo/code-agent',
        source: 'worker',
        event_type: 'worker_completed',
        observed_at: new Date().toISOString(),
        summary: 'Worker completed run',
        content: 'Detailed worker output',
        metadata_payload: {},
        privacy_stripped: false,
        admission_status: 'processed',
        decision_id: 'dec-1',
        proposal_id: 'mp-1',
        durable_memory_id: 'mem-1',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    vi.mocked(api.listMemoryAdmissionDecisions).mockResolvedValue([
      {
        decision_id: 'dec-1',
        category: 'project',
        memory_key: 'verification_commands',
        candidate_payload: { repo_url: 'https://github.com/natanayalo/code-agent' },
        decision: 'create',
        risk_level: 'low',
        reason: 'low-risk evidenced project memory can be created.',
        repo_url: 'https://github.com/natanayalo/code-agent',
        source_observation_id: 'obs-1',
        proposal_id: 'mp-1',
        durable_memory_id: 'mem-1',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);

    renderKnowledgeBasePage();

    fireEvent.click(await screen.findByRole('tab', { name: /Trace/i }));

    expect(await screen.findByText('Worker completed run')).toBeInTheDocument();
    expect(screen.getByText(/verification_commands/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Observation: obs-1/i).length).toBeGreaterThan(0);
    const badges = Array.from(document.querySelectorAll('.proposal-status-badge'));
    expect(badges.some((badge) => badge.classList.contains('status-processed'))).toBe(true);
    expect(badges.some((badge) => badge.classList.contains('status-create'))).toBe(true);
  });

  it('normalizes trace badge classes and guards empty values', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.listMemoryObservations).mockResolvedValue([
      {
        observation_id: 'obs-2',
        source: 'worker',
        event_type: 'worker_completed',
        observed_at: new Date().toISOString(),
        summary: 'Observation with empty status',
        content: 'trace details',
        metadata_payload: {},
        privacy_stripped: false,
        admission_status: '' as unknown as string,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    vi.mocked(api.listMemoryAdmissionDecisions).mockResolvedValue([
      {
        decision_id: 'dec-2',
        category: 'project',
        memory_key: 'verification_commands',
        candidate_payload: {},
        decision: 'needs_human_review',
        risk_level: 'low',
        reason: 'needs manual confirmation',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);

    renderKnowledgeBasePage();

    fireEvent.click(await screen.findByRole('tab', { name: /Trace/i }));

    expect(await screen.findByText('Observation with empty status')).toBeInTheDocument();
    const badges = Array.from(document.querySelectorAll('.proposal-status-badge'));
    expect(
      badges.some((badge) => badge.classList.contains('status-needs-human-review'))
    ).toBe(true);
    const emptyStatusBadge = screen
      .getByText('Observation with empty status')
      .closest('.knowledge-entry')
      ?.querySelector('.proposal-status-badge');
    expect(emptyStatusBadge).toHaveClass('status-default');
    expect(emptyStatusBadge).toHaveTextContent('');
  });

  it('passes trace filters through to observation and decision queries', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    fireEvent.change(await screen.findByLabelText('Project Repository URL'), {
      target: { value: 'https://github.com/natanayalo/code-agent' },
    });
    await waitForDebounce();

    fireEvent.click(screen.getByRole('tab', { name: /Trace/i }));

    fireEvent.change(screen.getByLabelText('Search Observations'), {
      target: { value: 'pytest' },
    });
    fireEvent.change(screen.getByLabelText('Observation Source'), {
      target: { value: 'worker' },
    });
    fireEvent.change(screen.getByLabelText('Observation Status'), {
      target: { value: 'processed' },
    });
    fireEvent.change(screen.getByLabelText('Decision Filter'), {
      target: { value: 'create' },
    });

    await waitForDebounce();

    await waitFor(() => {
      expect(api.listMemoryObservations).toHaveBeenLastCalledWith({
        repoUrl: 'https://github.com/natanayalo/code-agent',
        source: 'worker',
        admissionStatus: 'processed',
        query: 'pytest',
        limit: 25,
      });
    });
    await waitFor(() => {
      expect(api.listMemoryAdmissionDecisions).toHaveBeenLastCalledWith({
        repoUrl: 'https://github.com/natanayalo/code-agent',
        decision: 'create',
        limit: 25,
      });
    });
    expect(
      screen.getByText('Recent observation records for https://github.com/natanayalo/code-agent.')
    ).toBeInTheDocument();
  });

  it('renders trace enum filters as selects with valid options', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    fireEvent.click(await screen.findByRole('tab', { name: /Trace/i }));

    const sourceFilter = screen.getByLabelText('Observation Source');
    const statusFilter = screen.getByLabelText('Observation Status');
    const decisionFilter = screen.getByLabelText('Decision Filter');

    expect(sourceFilter.tagName).toBe('SELECT');
    expect(statusFilter.tagName).toBe('SELECT');
    expect(decisionFilter.tagName).toBe('SELECT');
    expect(screen.getByRole('option', { name: 'All sources' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Worker' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Processed' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Needs Human Review' })).toBeInTheDocument();
  });

  it('renders trace loading states while observation and decision queries are pending', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.listMemoryObservations).mockReturnValue(new Promise(() => []));
    vi.mocked(api.listMemoryAdmissionDecisions).mockReturnValue(new Promise(() => []));

    renderKnowledgeBasePage();

    fireEvent.click(await screen.findByRole('tab', { name: /Trace/i }));

    expect(screen.getByRole('button', { name: 'Refreshing...' })).toBeDisabled();
    expect(await screen.findByText('Loading observations...')).toBeInTheDocument();
    expect(screen.getByText('Loading admission decisions...')).toBeInTheDocument();
  });

  it('submits personal memory upsert payload', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.upsertPersonalMemory).mockResolvedValue({
      memory_id: 'pm-2',
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

    await screen.findByLabelText('Project Repository URL');
    openAddMemoryTab();
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
    await waitFor(() => expect(api.getKnowledgeBaseStats).toHaveBeenCalled());
    expect(api.upsertPersonalMemory).toHaveBeenCalledWith({
      memory_key: 'editor',
      value: { theme: 'light' },
      source: 'operator',
      scope: 'global',
      confidence: 0.25,
      requires_verification: false,
    });
    expect(screen.getByLabelText('Personal Memory Key')).toHaveValue('');
    expect(screen.getByLabelText('Personal Memory Value (JSON object)')).toHaveValue('{\n  \n}');
    expect(screen.getByLabelText('Personal Source')).toHaveValue('');
    expect(screen.getByLabelText('Personal Scope')).toHaveValue('');
    expect(Number((screen.getByLabelText('Personal Confidence (0.0-1.0)') as HTMLInputElement).value)).toBe(
      1
    );
    expect(screen.getAllByLabelText('Requires verification')[0]).toBeChecked();
  });

  it('renders review tab with pending memory proposals', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.listMemoryProposals).mockImplementation(async (status) => {
      if (status === 'pending_review') {
        return [
          {
            proposal_id: 'mp-pending',
            category: 'project',
            repo_url: 'https://github.com/natanayalo/code-agent',
            memory_key: 'verification_commands',
            value: { python: '.venv/bin/pytest tests/unit' },
            status: 'pending_review',
            confidence: 0.9,
            source: 'curated_corpus',
            scope: 'repo',
            requires_verification: false,
            title: 'Verification commands',
            summary: 'Use repo-local Python test commands.',
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
        ];
      }
      return [];
    });

    renderKnowledgeBasePage();

    fireEvent.click(await screen.findByRole('tab', { name: /Review/i }));

    expect(await screen.findByText('Verification commands')).toBeInTheDocument();
    expect(screen.getByText('Key: verification_commands')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Accept/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Reject/i })).toBeInTheDocument();
  });

  it('creates manual memory proposals instead of direct memory rows', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    fireEvent.change(await screen.findByLabelText('Project Repository URL'), {
      target: { value: 'https://github.com/natanayalo/code-agent' },
    });
    fireEvent.click(screen.getByRole('tab', { name: /Review/i }));
    fireEvent.change(screen.getByLabelText('Memory Key'), {
      target: { value: 'verification_commands' },
    });
    fireEvent.change(screen.getByLabelText('Title'), {
      target: { value: 'Verification commands' },
    });
    fireEvent.change(screen.getByLabelText('Summary'), {
      target: { value: 'Use repo-local commands.' },
    });
    fireEvent.change(screen.getByLabelText('Memory Value (JSON object)'), {
      target: { value: '{"python":".venv/bin/pytest tests/unit"}' },
    });
    fireEvent.change(screen.getByLabelText('Evidence (optional JSON object)'), {
      target: { value: '{"source":"AGENTS.md"}' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Create Memory Proposal' }));

    await waitFor(() => expect(api.createMemoryProposal).toHaveBeenCalledTimes(1));
    expect(api.createMemoryProposal).toHaveBeenCalledWith({
      category: 'project',
      repo_url: 'https://github.com/natanayalo/code-agent',
      memory_key: 'verification_commands',
      value: { python: '.venv/bin/pytest tests/unit' },
      title: 'Verification commands',
      summary: 'Use repo-local commands.',
      evidence: { source: 'AGENTS.md' },
      source: 'curated_corpus',
      scope: 'repo',
      confidence: 0.9,
      requires_verification: false,
    });
    expect(api.upsertProjectMemory).not.toHaveBeenCalled();
  });

  it('creates personal manual memory proposals without repo scope', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    fireEvent.click(await screen.findByRole('tab', { name: /Review/i }));
    fireEvent.change(screen.getByLabelText('Proposal Category'), {
      target: { value: 'personal' },
    });
    fireEvent.change(screen.getByLabelText('Memory Key'), {
      target: { value: 'communication_preferences' },
    });
    fireEvent.change(screen.getByLabelText('Memory Value (JSON object)'), {
      target: { value: '{"style":"concise"}' },
    });
    fireEvent.change(screen.getByLabelText('Source'), {
      target: { value: 'operator' },
    });
    fireEvent.change(screen.getByLabelText('Scope'), {
      target: { value: 'global' },
    });
    fireEvent.change(screen.getByLabelText('Confidence (0.0-1.0)'), {
      target: { value: '0.75' },
    });
    fireEvent.click(screen.getAllByLabelText('Requires verification')[0]);

    fireEvent.click(screen.getByRole('button', { name: 'Create Memory Proposal' }));

    await waitFor(() => expect(api.createMemoryProposal).toHaveBeenCalledTimes(1));
    expect(api.createMemoryProposal).toHaveBeenCalledWith({
      category: 'personal',
      repo_url: undefined,
      memory_key: 'communication_preferences',
      value: { style: 'concise' },
      title: undefined,
      summary: undefined,
      evidence: undefined,
      source: 'operator',
      scope: 'global',
      confidence: 0.75,
      requires_verification: true,
    });
  });

  it('accepts memory proposals and refreshes inventory', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.listMemoryProposals).mockImplementation(async (status) => {
      if (status === 'pending_review') {
        return [
          {
            proposal_id: 'mp-accept',
            category: 'personal',
            memory_key: 'communication_style',
            value: { style: 'concise' },
            status: 'pending_review',
            confidence: 0.95,
            requires_verification: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
        ];
      }
      return [];
    });

    renderKnowledgeBasePage();

    fireEvent.click(await screen.findByRole('tab', { name: /Review/i }));
    await screen.findByText('communication_style');
    fireEvent.click(screen.getByRole('button', { name: /Accept/i }));

    await waitFor(() => expect(api.acceptMemoryProposal).toHaveBeenCalledWith('mp-accept'));
    await waitFor(() => expect(api.getKnowledgeBaseStats).toHaveBeenCalled());
    expect(api.listPersonalMemory).toHaveBeenCalled();
    expect(api.listProjectMemory).toHaveBeenCalled();
  });

  it('rejects memory proposals without refreshing inventory', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);
    vi.mocked(api.listMemoryProposals).mockImplementation(async (status) => {
      if (status === 'pending_review') {
        return [
          {
            proposal_id: 'mp-reject',
            category: 'personal',
            memory_key: 'obsolete_memory',
            value: { note: 'skip' },
            status: 'pending_review',
            confidence: 0.5,
            requires_verification: true,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
        ];
      }
      return [];
    });

    renderKnowledgeBasePage();

    fireEvent.click(await screen.findByRole('tab', { name: /Review/i }));
    await screen.findByText('obsolete_memory');
    fireEvent.click(screen.getByRole('button', { name: /Reject/i }));

    await waitFor(() => expect(api.rejectMemoryProposal).toHaveBeenCalledWith('mp-reject'));
    expect(api.acceptMemoryProposal).not.toHaveBeenCalled();
  });

  it('shows validation error for non-object JSON values', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    await screen.findByLabelText('Project Repository URL');
    openAddMemoryTab();
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
    expect(api.listPersonalMemory).toHaveBeenCalled();
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
    openAddMemoryTab();
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
    await waitFor(() => expect(api.getKnowledgeBaseStats).toHaveBeenCalled());
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
    openAddMemoryTab();
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

    openAddMemoryTab();
    await screen.findByLabelText('Personal Confidence (0.0-1.0)');
    expect(screen.getByLabelText('Personal Confidence (0.0-1.0)')).toHaveAttribute('type', 'number');
    expect(screen.getByLabelText('Project Confidence (0.0-1.0)')).toHaveAttribute('type', 'number');
  });

  it('deletes a personal memory entry', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([
      {
        memory_id: 'pm-delete',
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

    await screen.findByText('remove_personal');
    fireEvent.click(screen.getByLabelText('Delete personal memory remove_personal'));

    await waitFor(() => {
      expect(api.deletePersonalMemory).toHaveBeenCalledWith('remove_personal');
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

  it('loads personal entries by default without a user filter', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    await waitFor(() => {
      expect(api.listPersonalMemory).toHaveBeenCalledWith(50, 0);
    });
    expect(screen.queryByLabelText('Personal User ID')).not.toBeInTheDocument();
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
    vi.mocked(api.listPersonalMemory).mockImplementation(async (limit, offset) =>
      Array.from({ length: limit ?? 0 }, (_, index) => ({
        memory_id: `pm-${offset}-${index}`,
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

    await waitFor(() => {
      expect(api.listPersonalMemory).toHaveBeenCalledWith(50, 0);
    });
    expect(await screen.findByText('entry-0')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Load More Personal Entries' }));

    await waitFor(() => {
      expect(api.listPersonalMemory).toHaveBeenCalledWith(50, 50);
    });
  });

  it('shows loaded totals and the max-loaded browse summary', async () => {
    vi.mocked(api.getKnowledgeBaseStats).mockResolvedValue({
      personal: { total: 4, requires_verification: 1 },
      project: null,
      project_global: { total: 250, requires_verification: 8 },
    });
    vi.mocked(api.listPersonalMemory).mockResolvedValue([
      {
        memory_id: 'pm-loaded-1',
        memory_key: 'loaded-one',
        value: { index: 1 },
        confidence: 1,
        scope: 'global',
        source: null,
        last_verified_at: null,
        requires_verification: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      {
        memory_id: 'pm-loaded-2',
        memory_key: 'loaded-two',
        value: { index: 2 },
        confidence: 1,
        scope: 'global',
        source: null,
        last_verified_at: null,
        requires_verification: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    vi.mocked(api.listProjectMemory).mockResolvedValue(
      Array.from({ length: 200 }, (_, index) => ({
        memory_id: `pj-max-${index}`,
        repo_url: 'https://github.com/natanayalo/code-agent',
        memory_key: `project-${index}`,
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

    expect(await screen.findByText('Loaded 2 of 4 personal memories.')).toBeInTheDocument();
    expect(
      await screen.findByText('Showing first 200 of 250 project memories.')
    ).toBeInTheDocument();
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

    await screen.findByLabelText('Project Repository URL');
    fireEvent.change(screen.getByLabelText('Project Repository URL'), {
      target: { value: 'https://github.com/natanayalo/code-agent' },
    });
    fireEvent.change(screen.getByLabelText('Search Query'), {
      target: { value: 'pytest' },
    });

    await waitForDebounce();
    await waitFor(() => {
      expect(api.searchPersonalMemory).toHaveBeenCalledWith('pytest', 20);
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

    await screen.findByLabelText('Project Repository URL');
    fireEvent.change(screen.getByLabelText('Search Query'), {
      target: { value: 'ed' },
    });
    await waitForDebounce();
    await waitFor(() => {
      expect(api.searchPersonalMemory).toHaveBeenCalledWith('ed', 20);
    });

    openAddMemoryTab();
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

  it('clears search back to browse mode immediately', async () => {
    vi.mocked(api.listPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.listProjectMemory).mockResolvedValue([]);
    vi.mocked(api.searchPersonalMemory).mockResolvedValue([]);
    vi.mocked(api.searchProjectMemory).mockResolvedValue([]);

    renderKnowledgeBasePage();

    await screen.findByLabelText('Project Repository URL');
    await waitFor(() => {
      expect(api.listPersonalMemory).toHaveBeenCalledWith(50, 0);
    });

    fireEvent.change(screen.getByLabelText('Search Query'), {
      target: { value: 'py' },
    });
    await waitForDebounce();
    await waitFor(() => {
      expect(api.searchPersonalMemory).toHaveBeenCalledWith('py', 20);
    });
    expect(screen.getByText(/Searching for/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Clear Search' }));

    expect(
      screen.getByText('Clear the query at any time to return to paginated browse mode.')
    ).toBeInTheDocument();
    expect(screen.getByText('No personal entries found.')).toBeInTheDocument();
  });
});
