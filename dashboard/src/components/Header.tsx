import React from 'react';
import { Layout } from 'lucide-react';

export function Header() {
  return (
    <header className="dashboard-header">
      <div className="icon-wrapper">
        <Layout size={24} color="var(--color-text-primary)" />
      </div>
      <div>
        <h1 className="dashboard-title gradient-text">Code Agent</h1>
        <p className="dashboard-subtitle">Operator Dashboard</p>
      </div>
    </header>
  );
}
