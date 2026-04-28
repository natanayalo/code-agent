import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { ReplayWithOverridesModal } from './ReplayWithOverridesModal';
import { api } from '../services/api';

vi.mock('../services/api', () => ({
  api: {
    replayTask: vi.fn(),
  },
}));

describe('ReplayWithOverridesModal', () => {
  beforeEach(() => {
    vi.mocked(api.replayTask).mockReset();
  });

  it('does not render when closed', () => {
    const { container } = render(
      <ReplayWithOverridesModal taskId="task-1" isOpen={false} onClose={vi.fn()} />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('submits with worker and JSON overrides', async () => {
    const onReplaySuccess = vi.fn();
    const onClose = vi.fn();
    vi.mocked(api.replayTask).mockResolvedValueOnce({ task_id: 'new-task' } as never);

    render(
      <ReplayWithOverridesModal
        taskId="task-1"
        isOpen={true}
        onClose={onClose}
        onReplaySuccess={onReplaySuccess}
      />
    );

    fireEvent.change(screen.getByLabelText('Worker Override'), {
      target: { value: 'gemini' },
    });
    fireEvent.change(screen.getByLabelText('Constraints Override (JSON object)'), {
      target: { value: '{"max_files": 5}' },
    });
    fireEvent.change(screen.getByLabelText('Budget Override (JSON object)'), {
      target: { value: '{"max_steps": 20}' },
    });
    fireEvent.change(screen.getByLabelText('Secrets Override (JSON object)'), {
      target: { value: '{"API_TOKEN": "abc"}' },
    });

    fireEvent.click(screen.getByRole('button', { name: /Replay Task/i }));

    await vi.waitFor(() =>
      expect(api.replayTask).toHaveBeenCalledWith('task-1', {
        worker_override: 'gemini',
        constraints: { max_files: 5 },
        budget: { max_steps: 20 },
        secrets: { API_TOKEN: 'abc' },
      })
    );
    expect(onReplaySuccess).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('shows field validation for malformed JSON', async () => {
    render(
      <ReplayWithOverridesModal taskId="task-1" isOpen={true} onClose={vi.fn()} />
    );

    fireEvent.change(screen.getByLabelText('Constraints Override (JSON object)'), {
      target: { value: '{"bad_json": }' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Replay Task/i }));

    expect(await screen.findByText('Constraints override must be valid JSON.')).toBeInTheDocument();
    expect(api.replayTask).not.toHaveBeenCalled();
  });

  it('shows secrets validation when values are not strings', async () => {
    render(
      <ReplayWithOverridesModal taskId="task-1" isOpen={true} onClose={vi.fn()} />
    );

    fireEvent.change(screen.getByLabelText('Secrets Override (JSON object)'), {
      target: { value: '{"API_TOKEN": 123}' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Replay Task/i }));

    expect(
      await screen.findByText('Secrets override values must be strings (invalid key: API_TOKEN).')
    ).toBeInTheDocument();
    expect(api.replayTask).not.toHaveBeenCalled();
  });

  it('renders API submission errors', async () => {
    vi.mocked(api.replayTask).mockRejectedValueOnce(new Error('Replay conflict'));

    render(
      <ReplayWithOverridesModal taskId="task-1" isOpen={true} onClose={vi.fn()} />
    );

    fireEvent.click(screen.getByRole('button', { name: /Replay Task/i }));

    expect(await screen.findByText('Replay conflict')).toBeInTheDocument();
  });

  it('does not close from backdrop clicks', () => {
    const onClose = vi.fn();
    const { container } = render(
      <ReplayWithOverridesModal taskId="task-1" isOpen={true} onClose={onClose} />
    );

    fireEvent.click(container.querySelector('.modal-overlay') as HTMLDivElement);

    expect(onClose).not.toHaveBeenCalled();
  });

  it('disables close controls while submitting', async () => {
    let resolveReplay: () => void;
    const replayPromise = new Promise<void>((resolve) => {
      resolveReplay = resolve;
    });
    vi.mocked(api.replayTask).mockReturnValueOnce(replayPromise as never);

    render(
      <ReplayWithOverridesModal taskId="task-1" isOpen={true} onClose={vi.fn()} />
    );

    fireEvent.click(screen.getByRole('button', { name: 'Replay Task' }));

    const closeButton = screen.getByRole('button', { name: 'Close replay override modal' });
    const cancelButton = screen.getByRole('button', { name: 'Cancel' });
    expect(closeButton).toBeDisabled();
    expect(cancelButton).toBeDisabled();

    // @ts-expect-error: resolveReplay is captured from promise constructor
    resolveReplay();
    await vi.waitFor(() => expect(closeButton).not.toBeDisabled());
  });
});
