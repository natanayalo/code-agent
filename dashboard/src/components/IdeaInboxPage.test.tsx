import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { IdeaInboxPage } from './IdeaInboxPage';
import { api, ApiError } from '../services/api';
import { ProposalStatus, ProposalSnapshot } from '../types/proposal';

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

const renderWithProviders = (ui: React.ReactElement) => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>
  );
};

describe('IdeaInboxPage', () => {
  beforeEach(() => {
    vi.resetAllMocks();
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

  it('renders proposals and handles accept', async () => {
    const mockProposals = [
      {
        proposal_id: 'p1',
        session_id: 's1',
        task_id: null,
        title: 'Idea 1',
        summary: 'Summary 1',
        content: null,
        status: ProposalStatus.PENDING_REVIEW,
        metadata_payload: {},
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ];

    vi.mocked(api.listProposals).mockResolvedValue(mockProposals);
    vi.mocked(api.acceptProposal).mockResolvedValue({ task_id: 't1', status: 'pending' } as unknown as import('../types/task').TaskSnapshot);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Idea 1')).toBeInTheDocument();
      expect(screen.getByText('Summary 1')).toBeInTheDocument();
    });

    const acceptBtn = screen.getByText('Accept Idea');
    fireEvent.click(acceptBtn);

    await waitFor(() => {
      expect(api.acceptProposal).toHaveBeenCalledWith('p1');
    });
  });

  it('renders proposals and handles reject with confirmation', async () => {
    const mockProposals = [
      {
        proposal_id: 'p2',
        session_id: 's1',
        task_id: null,
        title: 'Idea 2',
        summary: 'Summary 2',
        content: null,
        status: ProposalStatus.PENDING_REVIEW,
        metadata_payload: {},
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ];

    vi.mocked(api.listProposals).mockResolvedValue(mockProposals);
    vi.mocked(api.rejectProposal).mockResolvedValue({ ...mockProposals[0], status: ProposalStatus.REJECTED });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Idea 2')).toBeInTheDocument();
    });

    const rejectBtn = screen.getByText('Reject');
    fireEvent.click(rejectBtn);

    await waitFor(() => {
      expect(window.confirm).toHaveBeenCalledWith('Are you sure you want to reject this idea?');
      expect(api.rejectProposal).toHaveBeenCalledWith('p2');
    });
  });

  it('handles acceptProposal error and displays message', async () => {
    const p1 = { proposal_id: 'p1', session_id: 's1', task_id: null, title: 'Idea 1', summary: 'S1', content: null, status: ProposalStatus.PENDING_REVIEW, metadata_payload: {}, created_at: new Date().toISOString() };
    vi.mocked(api.listProposals).mockResolvedValue([p1 as ProposalSnapshot]);
    vi.mocked(api.acceptProposal).mockRejectedValue(new ApiError(400, 'Custom Accept Error'));

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Idea 1')).toBeInTheDocument();
    });

    const acceptBtn = screen.getByText('Accept Idea');
    fireEvent.click(acceptBtn);

    const errorMsg = await screen.findByText('Action failed: Custom Accept Error');
    expect(errorMsg).toBeInTheDocument();

    const dismissBtn = screen.getByText('Dismiss');
    fireEvent.click(dismissBtn);

    await waitFor(() => {
      expect(screen.queryByText('Action failed: Custom Accept Error')).not.toBeInTheDocument();
    });
  });

  it('handles rejectProposal error and displays message', async () => {
    const p2 = { proposal_id: 'p2', session_id: 's1', task_id: null, title: 'Idea 2', summary: 'S2', content: null, status: ProposalStatus.PENDING_REVIEW, metadata_payload: {}, created_at: new Date().toISOString() };
    vi.mocked(api.listProposals).mockResolvedValue([p2 as ProposalSnapshot]);
    vi.mocked(api.rejectProposal).mockRejectedValue(new Error('Generic reject error'));
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<IdeaInboxPage />);

    await waitFor(() => {
      expect(screen.getByText('Idea 2')).toBeInTheDocument();
    });

    const rejectBtn = screen.getByText('Reject');
    fireEvent.click(rejectBtn);

    const errorMsg = await screen.findByText('Action failed: Failed to reject proposal');
    expect(errorMsg).toBeInTheDocument();
  });

  it('handles generic list loading error and retry', async () => {
    vi.mocked(api.listProposals).mockRejectedValue('Generic error string');

    renderWithProviders(<IdeaInboxPage />);

    const errorMsg = await screen.findByText('Failed to load proposals: Generic error string');
    expect(errorMsg).toBeInTheDocument();

    const retryBtn = screen.getByText('Retry');
    fireEvent.click(retryBtn);
    expect(api.listProposals).toHaveBeenCalledTimes(2);
  });
});
