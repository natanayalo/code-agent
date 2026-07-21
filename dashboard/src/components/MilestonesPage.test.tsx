import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MilestonesPage } from './MilestonesPage';

const { api } = vi.hoisted(() => ({ api: {
  listMilestones: vi.fn(),
  listMilestoneReadinessAssessments: vi.fn(),
  decideMilestoneReadiness: vi.fn(),
} }));

vi.mock('../services/api', () => ({ api }));

function renderPage() {
  return render(
    <MemoryRouter><QueryClientProvider client={new QueryClient()}><MilestonesPage /></QueryClientProvider></MemoryRouter>,
  );
}

describe('MilestonesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listMilestones.mockResolvedValue([{ milestone_id: 'm25', key: 'M25.3', title: 'Cutover', status: 'active', active_autonomy_mode: 'human_led' }]);
    api.listMilestoneReadinessAssessments.mockResolvedValue([{ assessment_id: 'a1', completed_milestone_id: 'm25', status: 'pending_approval', reviewer_narrative: 'Read-only review', rubric: {}, recommended_mode: 'human_led' }]);
    api.decideMilestoneReadiness.mockResolvedValue({});
  });

  it('shows pending evidence and sends an explicit approval decision', async () => {
    renderPage();
    expect(await screen.findByText('Read-only review')).toBeInTheDocument();
    expect(screen.getByText('M25.3: Cutover')).toBeInTheDocument();
    screen.getByRole('button', { name: 'Approve human_led' }).click();
    await waitFor(() => expect(api.decideMilestoneReadiness).toHaveBeenCalledWith('a1', true, 'human_led'));
  });
});
