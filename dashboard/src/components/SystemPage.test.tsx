import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { SystemPage } from './SystemPage';
import { api } from '../services/api';

vi.mock('../services/api', () => ({
  api: {
    getSystemTools: vi.fn(),
    getSandboxStatus: vi.fn(),
    getRuntimeManifest: vi.fn(),
  },
}));

const runtimeManifestFixture = {
  service: {
    service_name: 'code-agent',
    schema_version: 1,
    environment: 'local',
    build_sha: null,
  },
  sandbox: {
    default_image: 'python:3.12-slim',
    workspace_root: '/tmp/workspaces',
  },
  worker: {
    worker_type: null,
    worker_profile: null,
    runtime_mode: null,
    workspace_id: null,
  },
  task: {
    read_only: false,
    network_enabled: false,
    delivery_mode: null,
    budget: {},
    allowed_actions: [],
    forbidden_actions: ['hardcode_secrets'],
    approval_required: false,
  },
  tools: [
    {
      name: 'execute_bash',
      capability_category: 'shell',
      side_effect_level: 'workspace_write',
      required_permission: 'workspace_write',
      network_required: false,
      deterministic: false,
    },
  ],
  approval_capabilities: ['clarification', 'permission', 'manual_approval'],
  maintenance_actions: [
    {
      action: 'restart_worker',
      description: 'Restart worker',
      request_only: true,
      requires_operator_approval: true,
    },
    {
      action: 'operator_attention',
      description: 'Ask operator',
      request_only: true,
      requires_operator_approval: true,
    },
  ],
};

function renderWithProviders(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>{ui}</BrowserRouter>
    </QueryClientProvider>
  );
  return { ...result, queryClient };
}

describe('SystemPage', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('renders loading states initially', () => {
    vi.mocked(api.getSystemTools).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.getSandboxStatus).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.getRuntimeManifest).mockReturnValue(new Promise(() => {}));

    renderWithProviders(<SystemPage />);

    expect(screen.getByText('Loading sandbox status...')).toBeInTheDocument();
    expect(screen.getByText('Loading runtime manifest...')).toBeInTheDocument();
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
    vi.mocked(api.getRuntimeManifest).mockResolvedValue(runtimeManifestFixture);

    const { container } = renderWithProviders(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText('python:3.12-slim')).toBeInTheDocument();
      expect(screen.getByText('/tmp/workspaces')).toBeInTheDocument();
      expect(screen.getByText('execute_bash')).toBeInTheDocument();
      expect(screen.getByText('code-agent v1')).toBeInTheDocument();
      expect(screen.getByText('Restart Worker, Operator Attention')).toBeInTheDocument();
      expect(screen.getByText('1 declared tools')).toBeInTheDocument();
    });

    // Verify table structure
    expect(screen.getByRole('table', { name: 'Tool Inventory' })).toBeInTheDocument();
    expect(screen.getByText('Shell')).toBeInTheDocument();
    expect(container.querySelector('.system-page-content')).toBeInTheDocument();
    expect(container.querySelector('.system-section-container')).toBeInTheDocument();
    expect(container.querySelector('.dashboard-content-inner')).not.toBeInTheDocument();
  });

  it('renders error states when API fails', async () => {
    vi.mocked(api.getSystemTools).mockRejectedValue(new Error('Failed to load tools'));
    vi.mocked(api.getSandboxStatus).mockRejectedValue(new Error('Failed to load sandbox'));
    vi.mocked(api.getRuntimeManifest).mockRejectedValue(new Error('Failed to load manifest'));

    renderWithProviders(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText('Failed to load sandbox status.')).toBeInTheDocument();
      expect(screen.getByText('Failed to load runtime manifest.')).toBeInTheDocument();
      expect(screen.getByText('Failed to load tool inventory.')).toBeInTheDocument();
    });

    const alerts = screen.getAllByRole('alert');
    expect(alerts).toHaveLength(3);
    expect(alerts[0]).toHaveClass('error-banner', 'system-error-banner');
    expect(alerts[1]).toHaveClass('error-banner', 'system-error-banner');
    expect(alerts[2]).toHaveClass('error-banner', 'system-error-banner');

    fireEvent.click(screen.getByRole('button', { name: 'Retry Sandbox' }));
    fireEvent.click(screen.getByRole('button', { name: 'Retry Manifest' }));
    fireEvent.click(screen.getByRole('button', { name: 'Retry Tools' }));

    await waitFor(() => {
      expect(api.getSandboxStatus).toHaveBeenCalledTimes(2);
      expect(api.getRuntimeManifest).toHaveBeenCalledTimes(2);
      expect(api.getSystemTools).toHaveBeenCalledTimes(2);
    });
  });

  it('disables retry buttons while refetching failed requests', async () => {
    let resolveToolsRetry: (value: []) => void = () => {};
    let resolveSandboxRetry: (value: { default_image: string; workspace_root: string }) => void =
      () => {};
    let resolveManifestRetry: (value: typeof runtimeManifestFixture) => void = () => {};

    vi.mocked(api.getSystemTools)
      .mockRejectedValueOnce(new Error('Failed to load tools'))
      .mockImplementationOnce(
        () => new Promise((resolve) => {
          resolveToolsRetry = resolve;
        })
      );
    vi.mocked(api.getSandboxStatus)
      .mockRejectedValueOnce(new Error('Failed to load sandbox'))
      .mockImplementationOnce(
        () => new Promise((resolve) => {
          resolveSandboxRetry = resolve;
        })
      );
    vi.mocked(api.getRuntimeManifest)
      .mockRejectedValueOnce(new Error('Failed to load manifest'))
      .mockImplementationOnce(
        () => new Promise((resolve) => {
          resolveManifestRetry = resolve;
        })
      );

    renderWithProviders(<SystemPage />);

    const sandboxRetry = await screen.findByRole('button', { name: 'Retry Sandbox' });
    const manifestRetry = screen.getByRole('button', { name: 'Retry Manifest' });
    const toolsRetry = screen.getByRole('button', { name: 'Retry Tools' });

    fireEvent.click(sandboxRetry);
    fireEvent.click(manifestRetry);
    fireEvent.click(toolsRetry);

    await waitFor(() => {
      expect(api.getSandboxStatus).toHaveBeenCalledTimes(2);
      expect(api.getRuntimeManifest).toHaveBeenCalledTimes(2);
      expect(api.getSystemTools).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      const retryingButtons = screen.getAllByRole('button', { name: 'Retrying...' });
      expect(retryingButtons).toHaveLength(3);
      expect(retryingButtons[0]).toBeDisabled();
      expect(retryingButtons[1]).toBeDisabled();
      expect(retryingButtons[2]).toBeDisabled();
    });

    resolveSandboxRetry({ default_image: 'python:3.12-slim', workspace_root: '/tmp/workspaces' });
    resolveManifestRetry(runtimeManifestFixture);
    resolveToolsRetry([]);

    await waitFor(() => {
      expect(screen.getByText('No tools registered.')).toBeInTheDocument();
      expect(screen.getByText('python:3.12-slim')).toBeInTheDocument();
      expect(screen.getByText('code-agent v1')).toBeInTheDocument();
    });
  });

  it('keeps cached system data visible when background refreshes fail', async () => {
    vi.mocked(api.getSystemTools)
      .mockResolvedValueOnce([
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
      ])
      .mockRejectedValueOnce(new Error('Failed to refresh tools'));
    vi.mocked(api.getSandboxStatus)
      .mockResolvedValueOnce({
        default_image: 'python:3.12-slim',
        workspace_root: '/tmp/workspaces'
      })
      .mockRejectedValueOnce(new Error('Failed to refresh sandbox'));
    vi.mocked(api.getRuntimeManifest)
      .mockResolvedValueOnce(runtimeManifestFixture)
      .mockRejectedValueOnce(new Error('Failed to refresh manifest'));

    const { queryClient } = renderWithProviders(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText('python:3.12-slim')).toBeInTheDocument();
      expect(screen.getByText('execute_bash')).toBeInTheDocument();
      expect(screen.getByText('code-agent v1')).toBeInTheDocument();
    });

    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['system-tools'] }),
      queryClient.invalidateQueries({ queryKey: ['system-sandbox'] }),
      queryClient.invalidateQueries({ queryKey: ['system-runtime-manifest'] }),
    ]);

    await waitFor(() => {
      expect(api.getSystemTools).toHaveBeenCalledTimes(2);
      expect(api.getSandboxStatus).toHaveBeenCalledTimes(2);
      expect(api.getRuntimeManifest).toHaveBeenCalledTimes(2);
    });

    expect(screen.getByText('python:3.12-slim')).toBeInTheDocument();
    expect(screen.getByText('execute_bash')).toBeInTheDocument();
    expect(screen.getByText('code-agent v1')).toBeInTheDocument();
    expect(screen.queryByText('Failed to load sandbox status.')).not.toBeInTheDocument();
    expect(screen.queryByText('Failed to load runtime manifest.')).not.toBeInTheDocument();
    expect(screen.queryByText('Failed to load tool inventory.')).not.toBeInTheDocument();
  });

  it('renders empty tool list gracefully', async () => {
    vi.mocked(api.getSystemTools).mockResolvedValue([]);
    vi.mocked(api.getSandboxStatus).mockResolvedValue({
      default_image: 'python:3.12-slim',
      workspace_root: '/tmp/workspaces'
    });
    vi.mocked(api.getRuntimeManifest).mockResolvedValue(runtimeManifestFixture);

    renderWithProviders(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText('No tools registered.')).toBeInTheDocument();
    });
  });

  it('renders read-only network-enabled manifest task defaults', async () => {
    vi.mocked(api.getSystemTools).mockResolvedValue([]);
    vi.mocked(api.getSandboxStatus).mockResolvedValue({
      default_image: 'python:3.12-slim',
      workspace_root: '/tmp/workspaces'
    });
    vi.mocked(api.getRuntimeManifest).mockResolvedValue({
      ...runtimeManifestFixture,
      task: {
        ...runtimeManifestFixture.task,
        read_only: true,
        network_enabled: true,
      },
      tools: [],
      maintenance_actions: [],
    });

    renderWithProviders(<SystemPage />);

    await waitFor(() => {
      expect(screen.getByText('Read only - Network enabled')).toBeInTheDocument();
      expect(screen.getByText('0 declared tools')).toBeInTheDocument();
    });
  });
});
