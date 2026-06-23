import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { IdeaInboxPage } from './IdeaInboxPage';
import { api, ApiError } from '../services/api';
import { ProposalStatus, ProposalSnapshot, ProposalType } from '../types/proposal';

vi.mock('../services/api', () => ({
  api: {
    listProposals: vi.fn(),
    acceptProposal: vi.fn(),
    rejectProposal: vi.fn(),
  },
  ApiError: class extends Error {
    constructor(public status: number, message: string) {
      super(message);
    }
  },
}));

const now = '2026-06-18T12:00:00.000Z';

const createProposal = (overrides: Partial<ProposalSnapshot> = {}): ProposalSnapshot => ({
  proposal_id: 'p1',
  session_id: 's1',
  task_id: null,
  title: 'Idea 1',
  summary: 'Summary 1',
  content: null,
  status: ProposalStatus.PENDING_REVIEW,
  proposal_type: ProposalType.SCOUT,
  metadata_payload: {},
  created_at: now,
  updated_at: now,
  ...overrides,
});

const renderWithProviders = (ui: React.ReactElement) => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
};

describe('IdeaInboxPage', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders loading state initially', () => {
    vi.mocked(api.listProposals).mockReturnValue(new Promise(() => {}));
    renderWithProviders(<IdeaInboxPage />);
    expect(screen.getByText('Loading proposals...')).toBeInTheDocument();
  });

  it('renders empty state when no proposals', async () => {
    vi.mocked(api.listProposals).mockResolvedValue([]);
    renderWithProviders(<IdeaInboxPage />);
    await waitFor(() => {
      expect(screen.getByText('No pending proposals')).toBeInTheDocument();
    });
  });

  it('formats proposal timestamps in UTC and falls back for malformed values', async () => {
    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({ proposal_id: 'p-valid-date', title: 'Dated idea' }),
      createProposal({
        proposal_id: 'p-invalid-date',
        title: 'Malformed date idea',
        created_at: 'invalid-date',
      }),
      createProposal({
        proposal_id: 'p-null-date',
        title: 'Missing date idea',
        created_at: null as unknown as string,
      }),
    ]);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Dated idea')).toBeInTheDocument();
    });

    expect(
      screen.getByText(
        new Date(now).toLocaleString('en-US', { timeZone: 'UTC', timeZoneName: 'short' }),
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText('N/A')).toHaveLength(2);
  });

  it('renders scout proposals and handles accept', async () => {
    const longToken = `edge-${'x'.repeat(72)}`;
    const longTitle = `Idea ${longToken}`;
    const longSummary = `Summary contains ${longToken}`;
    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({ proposal_id: 'p1', title: longTitle, summary: longSummary }),
    ]);
    vi.mocked(api.acceptProposal).mockResolvedValue({
      task_id: 't1',
      status: 'pending',
    } as unknown as import('../types/task').TaskSnapshot);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText(longTitle)).toHaveClass('memory-key');
      expect(screen.getByText(longSummary)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('Accept Idea'));

    await waitFor(() => {
      expect(api.acceptProposal).toHaveBeenCalledWith('p1');
    });
  });

  it('renders structured scout metadata as a reviewable proposal', async () => {
    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({
        proposal_id: 'p-structured-scout',
        title: 'Scout Output for Task 80671212-0736-426d-b23a-3ada4ba626ae',
        summary: 'Create a small skeleton so future tasks can start from a known baseline.',
        metadata_payload: {
          scout_focus: 'project bootstrap',
          scout_proposal: {
            title: 'Project Initialization & Skeleton Setup',
            description: 'Create a small skeleton so future tasks can start from a known baseline.',
            value: 'high',
            effort: 'small',
            risk: 'medium',
            layer_impact: 'worker',
            validation_path: 'Run targeted worker/orchestrator tests.',
            hitl_need: 'optional',
            evidence: ['AGENTS.md:1', 'workers/prompt.py:242'],
            implementation_slice: 'Add the skeleton setup task as a small implementation PR.',
          },
        },
      }),
    ]);

    renderWithProviders(<IdeaInboxPage />);

    expect(
      await screen.findByText('Project Initialization & Skeleton Setup'),
    ).toHaveClass('memory-key');
    expect(
      screen.queryByText('Scout Output for Task 80671212-0736-426d-b23a-3ada4ba626ae'),
    ).not.toBeInTheDocument();

    expect(
      screen.getByText('Create a small skeleton so future tasks can start from a known baseline.'),
    ).toHaveClass('proposal-summary', 'proposal-summary-scout');
    expect(
      within(screen.getByText('Value').closest('.proposal-score-item') as HTMLElement)
        .getByText('High'),
    ).toHaveClass('score-success');
    expect(
      within(screen.getByText('Effort').closest('.proposal-score-item') as HTMLElement)
        .getByText('Small'),
    ).toHaveClass('score-success');
    expect(
      within(screen.getByText('Risk').closest('.proposal-score-item') as HTMLElement)
        .getByText('Medium'),
    ).toHaveClass('score-medium');
    expect(
      within(screen.getByText('Layer').closest('.proposal-score-item') as HTMLElement)
        .getByText('Worker'),
    ).toHaveClass('score-neutral');
    expect(
      within(screen.getByText('HITL').closest('.proposal-score-item') as HTMLElement)
        .getByText('Optional'),
    ).toHaveClass('score-medium');
    expect(screen.getByText(/Run targeted worker\/orchestrator tests\./)).toBeInTheDocument();
    expect(screen.getByText(/Focus:/)).toBeInTheDocument();
    expect(screen.getByText('project bootstrap')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Scout evidence'));

    expect(screen.getByText('AGENTS.md:1')).toBeInTheDocument();
    expect(screen.getByText('workers/prompt.py:242')).toBeInTheDocument();
    expect(
      screen.getByText('Add the skeleton setup task as a small implementation PR.'),
    ).toHaveClass('proposal-text-block');
    expect(screen.queryByText('Scout Output:')).not.toBeInTheDocument();
  });

  it('renders legacy markdown scout output as a reviewable proposal fallback', async () => {
    const scoutSummary = [
      '[stdout truncated for summary]',
      '//Users/natanayalo/.code-agent/workspaces/example/AGENTS.md file touched.',
      '',
      '---',
      '',
      '### Dummy Proposal: Project Initialization & Skeleton Setup',
      '',
      'Create a small skeleton so future tasks can start from a known baseline.',
      '',
      '- **Value:** High',
      '- **Effort:** Small',
      '- **Risk:** Medium',
      '- **Layer:** Worker',
      '- **HITL:** Optional',
      '- **Validation:** Run targeted worker/orchestrator tests.',
    ].join('\n');

    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({
        proposal_id: 'p-formatted-scout',
        title: 'Scout Output for Task 80671212-0736-426d-b23a-3ada4ba626ae',
        summary: scoutSummary,
      }),
    ]);

    renderWithProviders(<IdeaInboxPage />);

    expect(
      await screen.findByText('Project Initialization & Skeleton Setup'),
    ).toHaveClass('memory-key');
    expect(
      screen.queryByText('Scout Output for Task 80671212-0736-426d-b23a-3ada4ba626ae'),
    ).not.toBeInTheDocument();

    const summaryBlock = screen.getByText(
      'Create a small skeleton so future tasks can start from a known baseline.',
    );
    expect(summaryBlock).toHaveClass('proposal-summary', 'proposal-summary-scout');
    expect(summaryBlock).not.toHaveClass('json-viewer');
    expect(screen.queryByText('[stdout truncated for summary]')).not.toBeInTheDocument();

    expect(
      within(screen.getByText('Value').closest('.proposal-score-item') as HTMLElement)
        .getByText('High'),
    ).toHaveClass('score-success');
    expect(
      within(screen.getByText('Effort').closest('.proposal-score-item') as HTMLElement)
        .getByText('Small'),
    ).toHaveClass('score-success');
    expect(
      within(screen.getByText('Risk').closest('.proposal-score-item') as HTMLElement)
        .getByText('Medium'),
    ).toHaveClass('score-medium');
    expect(
      within(screen.getByText('Layer').closest('.proposal-score-item') as HTMLElement)
        .getByText('Worker'),
    ).toHaveClass('score-neutral');
    expect(
      within(screen.getByText('HITL').closest('.proposal-score-item') as HTMLElement)
        .getByText('Optional'),
    ).toHaveClass('score-medium');
    expect(
      screen
        .getAllByText(/Run targeted worker\/orchestrator tests\./)
        .some((element) => element.classList.contains('proposal-validation-path')),
    ).toBe(true);

    fireEvent.click(screen.getByText('Scout evidence'));

    const rawOutputBlock = screen.getByText((content, element) => {
      return (
        element?.classList.contains('proposal-text-block') === true &&
        content.includes('[stdout truncated for summary]') &&
        content.includes('Project Initialization & Skeleton Setup')
      );
    });
    expect(rawOutputBlock).not.toHaveClass('json-viewer');
  });

  it('renders scout proposals and handles reject with confirmation', async () => {
    const proposal = createProposal({
      proposal_id: 'p2',
      title: 'Idea 2',
      summary: 'Summary 2',
    });

    vi.mocked(api.listProposals).mockResolvedValue([proposal]);
    vi.mocked(api.rejectProposal).mockResolvedValue({
      ...proposal,
      status: ProposalStatus.REJECTED,
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Idea 2')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('Reject'));

    await waitFor(() => {
      expect(window.confirm).toHaveBeenCalledWith('Are you sure you want to reject this idea?');
      expect(api.rejectProposal).toHaveBeenCalledWith('p2');
    });
  });

  it('handles acceptProposal error and displays message', async () => {
    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({ proposal_id: 'p1', title: 'Idea 1', summary: 'S1' }),
    ]);
    vi.mocked(api.acceptProposal).mockRejectedValue(new ApiError(400, 'Custom Accept Error'));

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Idea 1')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('Accept Idea'));

    const errorMsg = await screen.findByText('Action failed: Custom Accept Error');
    expect(errorMsg).toBeInTheDocument();

    fireEvent.click(screen.getByText('Dismiss'));

    await waitFor(() => {
      expect(screen.queryByText('Action failed: Custom Accept Error')).not.toBeInTheDocument();
    });
  });

  it('handles rejectProposal error and displays message', async () => {
    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({ proposal_id: 'p2', title: 'Idea 2', summary: 'S2' }),
    ]);
    vi.mocked(api.rejectProposal).mockRejectedValue(new Error('Generic reject error'));
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Idea 2')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('Reject'));

    const errorMsg = await screen.findByText('Action failed: Generic reject error');
    expect(errorMsg).toBeInTheDocument();
  });

  it('handles generic list loading error and retry', async () => {
    vi.mocked(api.listProposals).mockRejectedValue('Generic error string');

    renderWithProviders(<IdeaInboxPage />);

    const errorMsg = await screen.findByText('Failed to load proposals: Generic error string');
    expect(errorMsg).toBeInTheDocument();

    fireEvent.click(screen.getByText('Retry'));
    expect(api.listProposals).toHaveBeenCalledTimes(2);
  });

  it('renders complex scout metadata payload gracefully', async () => {
    const pComplex = createProposal({
      proposal_id: 'p-complex',
      title: 'Idea Complex',
      summary: 'S3',
      metadata_payload: {
        files_changed: ['fileA.ts', 'fileB.ts'],
        diff_text: '--- a/fileA.ts\n+++ b/fileA.ts',
      },
    });
    const pUnserializable = createProposal({
      proposal_id: 'p-unserializable',
      title: 'Idea Unserializable',
      summary: 'S4',
      metadata_payload: {
        files_changed: { some: 'object' },
        diff_text: { another: 'object' },
      },
      created_at: 'invalid-date',
    });

    vi.mocked(api.listProposals).mockResolvedValue([pComplex, pUnserializable]);
    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Idea Complex')).toBeInTheDocument();
    });

    expect(
      screen.getByText((content) => content.includes('fileA.ts') && content.includes('fileB.ts')),
    ).toBeInTheDocument();
    expect(screen.getByText((content) => content.includes('--- a/fileA.ts'))).toBeInTheDocument();
    expect(screen.getAllByText('Unserializable Object value')).toHaveLength(2);
    expect(screen.getAllByText('N/A').length).toBeGreaterThan(0);
  });

  it('renders explicit scout mode metadata correctly', async () => {
    const pMode = createProposal({
      proposal_id: 'p-mode',
      title: 'Idea Mode',
      metadata_payload: {
        scout_mode: 'research',
        scout_depth: 'deep',
        scout_focus: 'React Hooks',
      },
    });

    vi.mocked(api.listProposals).mockResolvedValue([pMode]);
    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Idea Mode')).toBeInTheDocument();
    });

    expect(screen.getByText('Mode')).toBeInTheDocument();
    expect(screen.getByText('Research')).toBeInTheDocument();
    expect(screen.getByText('Depth')).toBeInTheDocument();
    expect(screen.getByText('Deep')).toBeInTheDocument();
    expect(screen.getByText(/Focus:/)).toBeInTheDocument();
    expect(screen.getByText('React Hooks')).toBeInTheDocument();
  });

  it('does not render scout metadata details for explicit null fields', async () => {
    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({
        proposal_id: 'p-null-metadata',
        title: 'Idea with null metadata',
        metadata_payload: {
          files_changed: null,
          diff_text: null,
          scout_mode: null,
          scout_depth: null,
          scout_focus: null,
        },
      }),
    ]);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Idea with null metadata')).toBeInTheDocument();
    });

    expect(screen.queryByText('Scout evidence')).not.toBeInTheDocument();
    expect(screen.queryByText('null')).not.toBeInTheDocument();
  });

  it('renders reflection improvements with scoring fields and friction evidence', async () => {
    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({
        proposal_id: 'p-reflection',
        proposal_type: ProposalType.REFLECTION,
        title: 'Harden sandbox infrastructure recovery',
        summary: 'Retries should stop when sandbox startup fails repeatedly.',
        metadata_payload: {
          improvement_suggestion: {
            value: 'High',
            effort: 'Large',
            risk: 'Low',
            layer_impact: 'sandbox',
            validation_path: 'Run sandbox integration smoke.',
            hitl_need: 'optional',
          },
          friction_report: {
            source: 'sandbox',
            impact: 'blocked',
            description: 'Sandbox command timed out twice.',
            context: { failure_kind: 'timeout' },
          },
          scoring: {
            enabled: true,
            mode: 'deterministic',
            rationale: 'Repeated blocked runs are high-value cleanup candidates.',
          },
        },
      }),
    ]);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Harden sandbox infrastructure recovery')).toBeInTheDocument();
    });

    expect(screen.getByText('Improvement')).toBeInTheDocument();
    expect(screen.getByText('Approve Improvement')).toBeInTheDocument();
    expect(
      within(screen.getByText('Value').closest('.proposal-score-item') as HTMLElement)
        .getByText('High'),
    ).toHaveClass('score-success');
    expect(
      within(screen.getByText('Effort').closest('.proposal-score-item') as HTMLElement)
        .getByText('Large'),
    ).toHaveClass('score-warning');
    expect(
      within(screen.getByText('Risk').closest('.proposal-score-item') as HTMLElement)
        .getByText('Low'),
    ).toHaveClass('score-success');
    expect(
      within(screen.getByText('HITL').closest('.proposal-score-item') as HTMLElement)
        .getByText('Optional'),
    ).toHaveClass('score-medium');
    expect(
      within(screen.getByText('Layer').closest('.proposal-score-item') as HTMLElement)
        .getByText('Sandbox'),
    ).toHaveClass('score-neutral');
    expect(screen.getAllByText('Sandbox')).toHaveLength(2);
    expect(screen.getByText(/Run sandbox integration smoke\./)).toBeInTheDocument();
    expect(
      screen.getByText('Repeated blocked runs are high-value cleanup candidates.'),
    ).toBeInTheDocument();
    expect(screen.getByText('Sandbox command timed out twice.')).toBeInTheDocument();
  });

  it('uses warning tone for low-value reflection improvements', async () => {
    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({
        proposal_id: 'p-low-value',
        proposal_type: ProposalType.REFLECTION,
        title: 'Low value reflection improvement',
        metadata_payload: {
          improvement_suggestion: {
            value: 'Low',
          },
        },
      }),
    ]);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Low value reflection improvement')).toBeInTheDocument();
    });

    expect(
      within(screen.getByText('Value').closest('.proposal-score-item') as HTMLElement)
        .getByText('Low'),
    ).toHaveClass('score-warning');
  });

  it('does not render empty friction context evidence', async () => {
    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({
        proposal_id: 'p-empty-context',
        proposal_type: ProposalType.REFLECTION,
        title: 'Reflection with empty context',
        metadata_payload: {
          friction_report: {
            source: 'tooling',
            impact: 'slowed_down',
            context: {},
          },
        },
      }),
    ]);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Reflection with empty context')).toBeInTheDocument();
    });

    expect(screen.getByText('Tooling')).toBeInTheDocument();
    expect(screen.queryByText('Context')).not.toBeInTheDocument();
    expect(screen.queryByText('{}')).not.toBeInTheDocument();
  });

  it('renders sparse reflection metadata with safe fallback values', async () => {
    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({
        proposal_id: 'p-reflection-sparse',
        proposal_type: ProposalType.REFLECTION,
        title: 'Sparse reflection improvement',
        created_at: undefined as unknown as string,
        metadata_payload: {
          improvement_suggestion: {
            value: 7,
            effort: true,
          },
          friction_report: {
            source: null,
            impact: null,
          },
        },
      }),
    ]);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Sparse reflection improvement')).toBeInTheDocument();
    });

    expect(screen.getByText('7')).toBeInTheDocument();
    expect(screen.getByText('true')).toBeInTheDocument();
    expect(screen.getAllByText('Not set').length).toBeGreaterThanOrEqual(4);
    expect(screen.getByText('N/A')).toBeInTheDocument();
  });

  it('filters mixed proposals by proposal type without refetching', async () => {
    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({ proposal_id: 'p-scout', title: 'Scout cleanup idea' }),
      createProposal({
        proposal_id: 'p-reflection',
        proposal_type: ProposalType.REFLECTION,
        title: 'Reflection improvement',
      }),
    ]);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Scout cleanup idea')).toBeInTheDocument();
      expect(screen.getByText('Reflection improvement')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Show improvements proposals' }));
    expect(screen.queryByText('Scout cleanup idea')).not.toBeInTheDocument();
    expect(screen.getByText('Reflection improvement')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show ideas proposals' }));
    expect(screen.getByText('Scout cleanup idea')).toBeInTheDocument();
    expect(screen.queryByText('Reflection improvement')).not.toBeInTheDocument();
    expect(api.listProposals).toHaveBeenCalledWith(ProposalStatus.PENDING_REVIEW);
    expect(api.listProposals).toHaveBeenCalledTimes(1);
  });

  it('uses filter-specific empty state labels', async () => {
    vi.mocked(api.listProposals).mockResolvedValue([
      createProposal({
        proposal_id: 'p-reflection-only',
        proposal_type: ProposalType.REFLECTION,
        title: 'Reflection only',
      }),
    ]);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Reflection only')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Show ideas proposals' }));

    expect(screen.getByText('No pending ideas')).toBeInTheDocument();
    expect(screen.queryByText('No pending proposals')).not.toBeInTheDocument();
  });

  it('uses reflection-specific rejection confirmation copy', async () => {
    const proposal = createProposal({
      proposal_id: 'p-reflection',
      proposal_type: ProposalType.REFLECTION,
      title: 'Reflection improvement',
    });

    vi.mocked(api.listProposals).mockResolvedValue([proposal]);
    vi.mocked(api.rejectProposal).mockResolvedValue({
      ...proposal,
      status: ProposalStatus.REJECTED,
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Reflection improvement')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('Reject'));

    await waitFor(() => {
      expect(window.confirm).toHaveBeenCalledWith(
        'Are you sure you want to reject this improvement?',
      );
      expect(api.rejectProposal).toHaveBeenCalledWith('p-reflection');
    });
  });
});
