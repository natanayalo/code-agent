import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '../services/api';
import { ScheduleCreateForm } from './ScheduleCreateForm';

vi.mock('../services/api', async (importOriginal) => {
  const original = await importOriginal<typeof import('../services/api')>();
  return { ...original, api: { createSchedule: vi.fn() } };
});

function mount() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } });
  render(<QueryClientProvider client={client}><ScheduleCreateForm /></QueryClientProvider>);
}
function fill() {
  fireEvent.change(screen.getByLabelText('Schedule ID'), { target: { value: 'weekly' } });
  fireEvent.change(screen.getByLabelText('Repository key'), { target: { value: 'project' } });
  fireEvent.change(screen.getByLabelText('Purpose'), { target: { value: 'evidence_collection' } });
  fireEvent.change(screen.getByLabelText('Incident lookback (hours)'), { target: { value: '48' } });
  fireEvent.change(screen.getByLabelText('Local time'), { target: { value: '09:00' } });
  fireEvent.change(screen.getByLabelText('IANA timezone'), { target: { value: 'Asia/Jerusalem' } });
}

describe('ScheduleCreateForm', () => {
  beforeEach(() => vi.clearAllMocks());

  it('reviews explicit evidence settings and reports the returned active state', async () => {
    vi.mocked(api.createSchedule).mockResolvedValue({ id: 'weekly', paused: false } as Awaited<ReturnType<typeof api.createSchedule>>);
    mount(); fill();
    fireEvent.click(screen.getByRole('button', { name: 'Review schedule' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Select at least one weekday');
    fireEvent.click(screen.getByLabelText('monday'));
    fireEvent.click(screen.getByRole('button', { name: 'Review schedule' }));
    expect(screen.getByText('Operational incidents · 48 hours')).toBeInTheDocument();
    expect(api.createSchedule).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }));
    expect(await screen.findByRole('status')).toHaveTextContent('active');
    expect(api.createSchedule).toHaveBeenCalledWith('weekly', { version: 1, purpose: 'evidence_collection', repo_key: 'project', branch: null, weekdays: ['monday'], time: '09:00', timezone: 'Asia/Jerusalem', evidence: { source: 'operational_incidents', lookback_hours: 48 } });
  });

  it('retains the request through conflict, edit, unknown outcome, and retry', async () => {
    vi.mocked(api.createSchedule).mockRejectedValueOnce(new ApiError(409, 'conflict')).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ id: 'weekly-2', paused: true } as Awaited<ReturnType<typeof api.createSchedule>>);
    mount(); fill();
    fireEvent.click(screen.getByLabelText('monday'));
    fireEvent.click(screen.getByRole('button', { name: 'Review schedule' }));
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('conflicts');
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('Repository key')).toHaveValue('project');
    fireEvent.change(screen.getByLabelText('Schedule ID'), { target: { value: 'weekly-2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Review schedule' }));
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('outcome is unknown');
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }));
    expect(await screen.findByRole('status')).toHaveTextContent('paused');
    expect(api.createSchedule).toHaveBeenCalledTimes(3);
  });

  it('shows server validation errors and allows changing purpose and weekdays', async () => {
    vi.mocked(api.createSchedule).mockRejectedValueOnce(new ApiError(400, 'Repository key is not allowlisted'));
    mount(); fill();
    fireEvent.change(screen.getByLabelText('Purpose'), { target: { value: 'health_summary' } });
    fireEvent.click(screen.getByLabelText('monday'));
    fireEvent.click(screen.getByLabelText('monday'));
    fireEvent.click(screen.getByLabelText('tuesday'));
    fireEvent.click(screen.getByRole('button', { name: 'Review schedule' }));
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Repository key is not allowlisted');
    expect(api.createSchedule).toHaveBeenCalledWith('weekly', expect.not.objectContaining({ evidence: expect.anything() }));
  });
});
