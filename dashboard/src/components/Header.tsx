import React from 'react';
import { Layout, LogOut } from 'lucide-react';
import { NavLink } from 'react-router-dom';
import { useOptionalAuth } from './auth/AuthContext';

const mobileNavItems = [
  { label: 'Tasks', to: '/' },
  { label: 'Sessions', to: '/sessions' },
  { label: 'Triggers', to: '/triggers' },
  { label: 'Idea Inbox', to: '/proposals' },
  { label: 'Knowledge Base', to: '/knowledge-base' },
  { label: 'Metrics', to: '/metrics' },
  { label: 'System', to: '/system' },
  { label: 'Settings', to: '/settings' },
];

interface HeaderAuthControls {
  authenticated: boolean;
  logout: () => Promise<void>;
}

interface HeaderProps {
  auth?: HeaderAuthControls;
}

export function Header({ auth: authOverride }: HeaderProps = {}) {
  const contextAuth = useOptionalAuth();
  const auth = authOverride ?? contextAuth;
  const [isLoggingOut, setIsLoggingOut] = React.useState(false);

  const handleLogout = async () => {
    if (!auth || isLoggingOut) {
      return;
    }
    setIsLoggingOut(true);
    try {
      await auth.logout();
    } finally {
      setIsLoggingOut(false);
    }
  };

  return (
    <header className="dashboard-header">
      <div className="dashboard-header-brand">
        <div className="icon-wrapper">
          <Layout size={24} color="var(--color-text-primary)" />
        </div>
        <div>
          <h1 className="dashboard-title gradient-text">Code Agent</h1>
          <p className="dashboard-subtitle">Operator Dashboard</p>
        </div>
      </div>
      <div className="dashboard-header-controls">
        {auth?.authenticated ? (
          <button
            type="button"
            className="header-logout-button"
            onClick={handleLogout}
            disabled={isLoggingOut}
          >
            <LogOut size={16} />
            <span>{isLoggingOut ? 'Logging out...' : 'Log out'}</span>
          </button>
        ) : null}
        <nav className="mobile-route-nav" aria-label="Dashboard sections">
          {mobileNavItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) => `mobile-route-link ${isActive ? 'active' : ''}`}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </div>
    </header>
  );
}
