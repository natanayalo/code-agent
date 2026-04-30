import React from 'react';
import { NavLink } from 'react-router-dom';
import { LayoutDashboard, MessageSquare, Settings, Shield, Activity, BookOpen, Server } from 'lucide-react';

interface SidebarItemProps {
  icon: React.ReactNode;
  label: string;
  to: string;
}

function SidebarItem({ icon, label, to }: SidebarItemProps) {
  return (
    <NavLink
      to={to}
      end={to === '/'}
      className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`}
    >
      <div className="sidebar-icon">{icon}</div>
      <span className="sidebar-label">{label}</span>
    </NavLink>
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
          <SidebarItem icon={<LayoutDashboard size={20} />} label="Tasks" to="/" />
          <SidebarItem icon={<MessageSquare size={20} />} label="Sessions" to="/sessions" />
          <SidebarItem icon={<BookOpen size={20} />} label="Knowledge Base" to="/knowledge-base" />
          <SidebarItem icon={<Activity size={20} />} label="Metrics" to="/metrics" />
        </div>

        <div className="nav-group">
          <div className="nav-group-label">System</div>
          <SidebarItem icon={<Server size={20} />} label="System Config" to="/system" />
          <SidebarItem icon={<Settings size={20} />} label="Settings" to="/settings" />
        </div>
      </nav>

      <div className="sidebar-footer">
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
