import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { TaskInteractionSection } from './TaskInteractionSection';
import { api } from '../services/api';

// Mock api
vi.mock('../services/api', () => ({
  api: {
    respondToInteraction: vi.fn(),
  },
}));

describe('TaskInteractionSection', () => {
  const mockTask = { task_id: 'task-1' } as any;
  const mockInteraction = {
    interaction_id: 'int-1',
    interaction_type: 'clarification',
    status: 'pending',
    summary: 'Need more info',
    data: { questions: ['Question 1?'] },
  } as any;

  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('renders pending interaction details', () => {
    render(
      <TaskInteractionSection
        task={mockTask}
        interaction={mockInteraction}
      />
    );

    expect(screen.getByText('Clarification Required')).toBeDefined();
    expect(screen.getByText('Need more info')).toBeDefined();
    expect(screen.getByText('Question 1?')).toBeDefined();
    expect(screen.getByPlaceholderText('Type your response here...')).toBeDefined();
  });

  it('submits response and calls onRefresh', async () => {
    const onRefresh = vi.fn();
    (api.respondToInteraction as any).mockResolvedValueOnce({ status: 'applied' });

    render(
      <TaskInteractionSection
        task={mockTask}
        interaction={mockInteraction}
        onRefresh={onRefresh}
      />
    );

    const textarea = screen.getByPlaceholderText('Type your response here...');
    fireEvent.change(textarea, { target: { value: 'My response' } });

    const submitButton = screen.getByText('Send Response');
    fireEvent.click(submitButton);

    await waitFor(() => {
      expect(api.respondToInteraction).toHaveBeenCalledWith('task-1', 'int-1', 'resolved', {
        text: 'My response',
      });
      expect(onRefresh).toHaveBeenCalled();
    });
  });

  it('displays error on submission failure', async () => {
    (api.respondToInteraction as any).mockRejectedValueOnce(new Error('API Error'));

    render(
      <TaskInteractionSection
        task={mockTask}
        interaction={mockInteraction}
      />
    );

    const textarea = screen.getByPlaceholderText('Type your response here...');
    fireEvent.change(textarea, { target: { value: 'My response' } });

    const submitButton = screen.getByText('Send Response');
    fireEvent.click(submitButton);

    await waitFor(() => {
      expect(screen.getByText('API Error')).toBeDefined();
    });
  });

  it('renders nothing if interaction is not pending', () => {
    const resolvedInteraction = { ...mockInteraction, status: 'resolved' };
    const { container } = render(
      <TaskInteractionSection
        task={mockTask}
        interaction={resolvedInteraction}
      />
    );

    expect(container.firstChild).toBeNull();
  });
});
