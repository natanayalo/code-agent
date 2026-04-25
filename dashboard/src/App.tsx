import React from 'react';
import { Layout, Play, CheckCircle, Clock, AlertCircle } from 'lucide-react';

function App() {
  return (
    <div className="dashboard-container">
      <header className="dashboard-header">
        <div className="icon-wrapper">
          <Layout size={24} color="white" />
        </div>
        <div>
          <h1 className="dashboard-title gradient-text">Code Agent</h1>
          <p className="dashboard-subtitle">Operator Dashboard</p>
        </div>
      </header>

      <main className="dashboard-main">
        {/* Placeholder Task Card */}
        <div className="glass-panel task-card">
          <div className="card-header">
            <span className="status-badge">Running</span>
            <Clock size={16} color="var(--color-text-muted)" />
          </div>
          <h3 className="task-title">Implement PWA Frontend Architecture</h3>
          <p className="task-description">
            Designing the core structure and selecting technology stack for the operator dashboard.
          </p>
          <div className="card-footer">
            <div className="task-meta">
              <Play size={14} />
              <span>3 commands run</span>
            </div>
          </div>
        </div>

        {/* Stats placeholder */}
        <div className="glass-panel stats-card">
          <div className="stats-item">
            <div className="success-icon"><CheckCircle size={20} /></div>
            <div>
              <div className="stats-value">124</div>
              <div className="stats-label">Tasks Completed</div>
            </div>
          </div>
          <div className="stats-item">
            <div className="error-icon"><AlertCircle size={20} /></div>
            <div>
              <div className="stats-value">2</div>
              <div className="stats-label">Failed Runs</div>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

export default App;
