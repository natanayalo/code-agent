import React, { useMemo } from 'react';
import { Clock, Terminal, Github, GitBranch } from 'lucide-react';
import { TaskSummarySnapshot, TaskStatus } from '../types/task';

interface TaskCardProps {
  task: TaskSummarySnapshot;
  onClick?: () => void;
}

const formatDate = (dateString: string) => {
  if (!dateString) return 'N/A';
  const date = new Date(dateString);
  if (isNaN(date.getTime())) return 'N/A';

  const now = new Date();
  const isToday = date.toDateString() === now.toDateString();

  return isToday
    ? date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
};

const getStatusClass = (status: TaskStatus) => {
  switch (status) {
    case TaskStatus.IN_PROGRESS: return 'status-running';
    case TaskStatus.COMPLETED: return 'status-success';
    case TaskStatus.FAILED:
    case TaskStatus.CANCELLED: return 'status-error';
    default: return 'status-pending';
  }
};

export function TaskCard({ task, onClick }: TaskCardProps) {
  const repoName = useMemo(() => {
    if (!task.repo_url) return 'Unknown Repo';
    try {
      const url = new URL(task.repo_url);
      const pathParts = url.pathname.split('/').filter(Boolean);
      if (pathParts.length === 0) return 'Unknown Repo';
      const lastPart = pathParts[pathParts.length - 1];
      return lastPart.replace(/\.git$/i, '');
    } catch {
      return 'Unknown Repo';
    }
  }, [task.repo_url]);

  return (
    <div className={`glass-panel task-card ${onClick ? 'task-card-clickable' : ''}`} onClick={onClick}>
      <div className="card-header">
        <span className={`status-badge ${getStatusClass(task.status)}`}>
          {task.status.replace('_', ' ')}
        </span>
        <div className="task-time">
          <Clock size={12} />
          <span>{formatDate(task.created_at)}</span>
        </div>
      </div>

      <h3 className="task-title-text">{task.task_text}</h3>

      <div className="task-details">
        {task.repo_url && (
          <div className="detail-item">
            <Github size={14} />
            <span className="truncate">{repoName}</span>
          </div>
        )}
        {task.branch && (
          <div className="detail-item">
            <GitBranch size={14} />
            <span>{task.branch}</span>
          </div>
        )}
      </div>

      <div className="card-footer">
        <div className="task-meta">
          <Terminal size={14} />
          <span>{task.latest_run_worker || task.chosen_worker || 'auto'}</span>
        </div>
        {task.latest_run_status && (
          <div className={`run-status ${task.latest_run_status}`}>
            {task.latest_run_status}
          </div>
        )}
      </div>
    </div>
  );
}
