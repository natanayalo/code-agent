import React from 'react';
import { CheckCircle, AlertCircle } from 'lucide-react';

interface StatsPanelProps {
  completed: number;
  failed: number;
}

export function StatsPanel({ completed, failed }: StatsPanelProps) {
  return (
    <div className="glass-panel stats-card">
      <div className="stats-item">
        <div className="success-icon"><CheckCircle size={20} /></div>
        <div>
          <div className="stats-value">{completed}</div>
          <div className="stats-label">Tasks Completed</div>
        </div>
      </div>
      <div className="stats-item">
        <div className="error-icon"><AlertCircle size={20} /></div>
        <div>
          <div className="stats-value">{failed}</div>
          <div className="stats-label">Failed Runs</div>
        </div>
      </div>
    </div>
  );
}
