import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Server, Wrench, Shield, HardDrive, AlertTriangle } from 'lucide-react';
import { DashboardLayout } from './layout/DashboardLayout';
import { api } from '../services/api';

export function SystemPage() {
  const { data: tools, isLoading: toolsLoading, error: toolsError } = useQuery({
    queryKey: ['system-tools'],
    queryFn: () => api.getSystemTools(),
  });

  const { data: sandbox, isLoading: sandboxLoading, error: sandboxError } = useQuery({
    queryKey: ['system-sandbox'],
    queryFn: () => api.getSandboxStatus(),
  });

  return (
    <DashboardLayout>
      <div className="dashboard-content-inner">
        <div className="panel-header" style={{ marginBottom: '1.5rem' }}>
          <h2 style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: 'var(--color-text-primary)' }}>
            <Server size={24} />
            System Configuration
          </h2>
          <p style={{ color: 'var(--color-text-secondary)', marginTop: '0.5rem' }}>
            View runtime capabilities and sandbox constraints.
          </p>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
          <section className="dashboard-card">
            <h3 style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1rem' }}>
              <HardDrive size={20} color="var(--color-accent-primary)" />
              Sandbox Status
            </h3>
            {sandboxLoading ? (
              <div className="loading-spinner">Loading sandbox status...</div>
            ) : sandboxError ? (
              <div className="error-message">
                <AlertTriangle size={16} /> Failed to load sandbox status.
              </div>
            ) : sandbox ? (
              <dl className="key-value-list" style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '1rem', background: 'var(--color-background-elevated)', padding: '1.5rem', borderRadius: '8px', margin: 0 }}>
                <dt style={{ color: 'var(--color-text-secondary)', fontWeight: 500 }}>Default Image</dt>
                <dd style={{ fontFamily: 'monospace', color: 'var(--color-text-primary)', margin: 0 }}>{sandbox.default_image || 'None'}</dd>

                <dt style={{ color: 'var(--color-text-secondary)', fontWeight: 500 }}>Workspace Root</dt>
                <dd style={{ fontFamily: 'monospace', color: 'var(--color-text-primary)', margin: 0 }}>{sandbox.workspace_root || 'None'}</dd>
              </dl>
            ) : null}
          </section>

          <section className="dashboard-card">
            <h3 style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1rem' }}>
              <Wrench size={20} color="var(--color-accent-primary)" />
              Tool Inventory
            </h3>
            {toolsLoading ? (
              <div className="loading-spinner">Loading tool inventory...</div>
            ) : toolsError ? (
              <div className="error-message">
                <AlertTriangle size={16} /> Failed to load tool inventory.
              </div>
            ) : tools && tools.length > 0 ? (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }} aria-label="Tool Inventory">
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--color-border)', color: 'var(--color-text-secondary)' }}>
                      <th style={{ padding: '0.75rem 1rem', fontWeight: 500 }}>Name</th>
                      <th style={{ padding: '0.75rem 1rem', fontWeight: 500 }}>Category</th>
                      <th style={{ padding: '0.75rem 1rem', fontWeight: 500 }}>Permission</th>
                      <th style={{ padding: '0.75rem 1rem', fontWeight: 500 }}>Side Effects</th>
                      <th style={{ padding: '0.75rem 1rem', fontWeight: 500 }}>Network</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tools.map(tool => (
                      <tr key={tool.name} style={{ borderBottom: '1px solid var(--color-border-subtle)' }}>
                        <td style={{ padding: '0.75rem 1rem', fontFamily: 'monospace', color: 'var(--color-accent-secondary)' }}>{tool.name}</td>
                        <td style={{ padding: '0.75rem 1rem' }}>
                          <span style={{ background: 'var(--color-background)', padding: '0.25rem 0.5rem', borderRadius: '4px', fontSize: '0.85rem' }}>
                            {tool.capability_category}
                          </span>
                        </td>
                        <td style={{ padding: '0.75rem 1rem' }}>
                          <span style={{
                            display: 'inline-flex', alignItems: 'center', gap: '0.25rem',
                            color: tool.required_permission === 'read_only' ? 'var(--color-text-secondary)' : 'var(--color-warning)'
                          }}>
                            {tool.required_permission !== 'read_only' && <Shield size={14} />}
                            {tool.required_permission}
                          </span>
                        </td>
                        <td style={{ padding: '0.75rem 1rem', color: 'var(--color-text-secondary)' }}>{tool.side_effect_level}</td>
                        <td style={{ padding: '0.75rem 1rem', color: tool.network_required ? 'var(--color-warning)' : 'var(--color-text-secondary)' }}>
                          {tool.network_required ? 'Required' : 'None'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div style={{ color: 'var(--color-text-secondary)', padding: '1rem', textAlign: 'center' }}>
                No tools registered.
              </div>
            )}
          </section>
        </div>
      </div>
    </DashboardLayout>
  );
}
