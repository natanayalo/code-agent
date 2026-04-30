import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { SystemPage } from './SystemPage';
import { api } from '../services/api';

vi.mock('../services/api', () => ({
  api: {
    getSystemTools: vi.fn(),
    getSandboxStatus: vi.fn(),
  },
}));

function renderWithProviders(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>{ui}</BrowserRouter>
    </QueryClientProvider>
  );
}

describe('SystemPage', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('renders loading states initially', () => {
    vi.mocked(api.getSystemTools).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.getSandboxStatus).mockReturnValue(new Promise(() => {}));

    renderWithProviders(<SystemPage />);

    expect(screen.getByText('Loading sandbox status...')).toBeInTheDocument();
    expect(screen.getByText('Loading tool inventory...')).toBeInTheDocument();
  });

  it('renders system configuration with data', async () => {
    vi.mocked(api.getSystemTools).mockResolvedValue([
      {
        name: 'execute_bash',
        description: 'Run bash command',
        capability_category: 'shell',
        side_effect_level: 'workspace_write',
        required_permission: 'workspace_write',
        timeout_seconds: 60,
        network_required: false,
        expected_artifacts: [],
        required_secrets: [],
        deterministic: false
      }
    ]);

    vi.mocked(api.getSandboxStatus).mockResolvedValue({
      default_image: 'python:3.12-slim',
      workspace_root: '/tmp/workspaces'
    });

    renderWithProviders(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText('python:3.12-slim')).toBeInTheDocument();
      expect(screen.getByText('/tmp/workspaces')).toBeInTheDocument();
      expect(screen.getByText('execute_bash')).toBeInTheDocument();
    });

    // Verify table structure
    expect(screen.getByRole('table', { name: 'Tool Inventory' })).toBeInTheDocument();
    expect(screen.getByText('Shell')).toBeInTheDocument();
  });

  it('renders error states when API fails', async () => {
    vi.mocked(api.getSystemTools).mockRejectedValue(new Error('Failed to load tools'));
    vi.mocked(api.getSandboxStatus).mockRejectedValue(new Error('Failed to load sandbox'));

    renderWithProviders(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText('Failed to load sandbox status.')).toBeInTheDocument();
      expect(screen.getByText('Failed to load tool inventory.')).toBeInTheDocument();
    });
  });

  it('renders empty tool list gracefully', async () => {
    vi.mocked(api.getSystemTools).mockResolvedValue([]);
    vi.mocked(api.getSandboxStatus).mockResolvedValue({
      default_image: 'python:3.12-slim',
      workspace_root: '/tmp/workspaces'
    });

    renderWithProviders(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText('No tools registered.')).toBeInTheDocument();
    });
  });
});
