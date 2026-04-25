import React from 'react';
import { Play, Clock } from 'lucide-react';

interface TaskCardProps {
  status: string;
  title: string;
  description: string;
  commandsRun: number;
}

export function TaskCard({ status, title, description, commandsRun }: TaskCardProps) {
  return (
    <div className="glass-panel task-card">
      <div className="card-header">
        <span className="status-badge">{status}</span>
        <Clock size={16} color="var(--color-text-muted)" />
      </div>
      <h3 className="task-title">{title}</h3>
      <p className="task-description">{description}</p>
      <div className="card-footer">
        <div className="task-meta">
          <Play size={14} />
          <span>{commandsRun} commands run</span>
        </div>
      </div>
    </div>
  );
}
