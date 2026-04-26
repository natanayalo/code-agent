import React from 'react';
import { Sidebar } from './Sidebar';
import { Header } from '../Header';

interface DashboardLayoutProps {
  children: React.ReactNode;
}

export function DashboardLayout({ children }: DashboardLayoutProps) {
  return (
    <div className="dashboard-app-container">
      <Sidebar />
      <div className="dashboard-content-wrapper">
        <Header />
        <main className="dashboard-content">
          {children}
        </main>
      </div>
    </div>
  );
}
