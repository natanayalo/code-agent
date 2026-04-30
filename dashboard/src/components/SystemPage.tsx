import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Server, Wrench, Shield, HardDrive, AlertTriangle } from 'lucide-react';
import { DashboardLayout } from './layout/DashboardLayout';
import { api } from '../services/api';
import { getPermissionStyle, getNetworkStyle, getCategoryThemeClass } from '../utils/styleHelpers';
import { formatLabel } from '../utils/formatters';

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

        <div className="system-section-container">
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
              <dl className="key-value-list">
                <dt className="key-value-dt">Default Image</dt>
                <dd className="key-value-dd">{sandbox.default_image || 'None'}</dd>

                <dt className="key-value-dt">Workspace Root</dt>
                <dd className="key-value-dd">{sandbox.workspace_root || 'None'}</dd>
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
              <div className="inventory-table-container">
                <table className="inventory-table" aria-label="Tool Inventory">
                  <thead>
                    <tr className="inventory-table-tr" style={{ color: 'var(--color-text-secondary)' }}>
                      <th className="inventory-table-th">Name</th>
                      <th className="inventory-table-th">Category</th>
                      <th className="inventory-table-th">Permission</th>
                      <th className="inventory-table-th">Side Effects</th>
                      <th className="inventory-table-th">Network</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tools.map((tool, index) => {
                      const permissionStyle = getPermissionStyle(tool.required_permission);
                      const networkStyle = getNetworkStyle(tool.network_required);
                      return (
                        <tr key={tool.name + "-" + index} className="inventory-table-tr">
                          <td className="inventory-table-td inventory-table-name">{tool.name}</td>
                          <td className="inventory-table-td">
                            <span className={getCategoryThemeClass(tool.capability_category)}>
                              {formatLabel(tool.capability_category)}
                            </span>
                          </td>
                          <td className="inventory-table-td">
                            <span className="permission-cell" style={{ color: permissionStyle.color }}>
                              {permissionStyle.showShield && <Shield size={14} />}
                              {formatLabel(tool.required_permission)}
                            </span>
                          </td>
                          <td className="inventory-table-td" style={{ color: 'var(--color-text-secondary)' }}>{formatLabel(tool.side_effect_level)}</td>
                          <td className="inventory-table-td" style={{ color: networkStyle.color }}>
                            {networkStyle.label}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="empty-state">
                No tools registered.
              </div>
            )}
          </section>
        </div>
      </div>
    </DashboardLayout>
  );
}
