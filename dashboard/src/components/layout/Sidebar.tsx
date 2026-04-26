import React from 'react';
import { LayoutDashboard, MessageSquare, Settings, Shield, Activity } from 'lucide-react';

interface SidebarItemProps {
  icon: React.ReactNode;
  label: string;
  active?: boolean;
  onClick?: () => void;
}

function SidebarItem({ icon, label, active, onClick }: SidebarItemProps) {
  return (
    <button
      className={`sidebar-item ${active ? 'active' : ''}`}
      onClick={onClick}
    >
      <div className="sidebar-icon">{icon}</div>
      <span className="sidebar-label">{label}</span>
    </button>
  );
}

export function Sidebar() {
  return (
    <aside className="dashboard-sidebar">
      <div className="sidebar-brand">
        <div className="brand-icon">
          <Shield size={24} color="var(--color-accent-primary)" />
        </div>
        <span className="brand-name gradient-text">Code Agent</span>
      </div>

      <nav className="sidebar-nav">
        <div className="nav-group">
          <div className="nav-group-label">Operations</div>
          <SidebarItem icon={<LayoutDashboard size={20} />} label="Tasks" active />
          <SidebarItem icon={<MessageSquare size={20} />} label="Sessions" />
          <SidebarItem icon={<Activity size={20} />} label="Metrics" />
        </div>

        <div className="nav-group">
          <div className="nav-group-label">System</div>
          <SidebarItem icon={<Settings size={20} />} label="Settings" />
        </div>
      </nav>

      <div className="sidebar-footer">
        {import.meta.env.DEV && (
          <div className="dev-warning">
            <span className="warning-pill">DEV MODE</span>
            <p className="warning-text">
              <strong>Security Alert:</strong> LocalStorage & VITE_ env vars are insecure for secrets.
              Production must use HttpOnly cookies/OIDC (Milestone 13).
            </p>
          </div>
        )}
        <div className="user-profile">
          <div className="user-avatar">OP</div>
          <div className="user-info">
            <div className="user-name">Operator</div>
            <div className="user-role">System Admin</div>
          </div>
        </div>
      </div>
    </aside>
  );
}
