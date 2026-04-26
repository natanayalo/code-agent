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

const deriveRepoName = (repoUrl: string) => {
  try {
    const url = new URL(repoUrl);
    const pathParts = url.pathname.split('/').filter(Boolean);
    if (pathParts.length === 0) return 'Unknown Repo';
    return pathParts[pathParts.length - 1].replace(/\.git$/i, '');
  } catch {
    // Support common non-URL git formats like git@github.com:owner/repo.git
    const normalized = repoUrl.trim().replace(/[:/]+$/, '');
    const pathParts = normalized.split(/[/:]/).filter(Boolean);
    if (pathParts.length === 0) return 'Unknown Repo';
    return pathParts[pathParts.length - 1].replace(/\.git$/i, '') || 'Unknown Repo';
  }
};

const getRunStatusClass = (status: string | null | undefined) => {
  if (!status) return '';
  switch (status) {
    case TaskStatus.COMPLETED:
      return 'success';
    case TaskStatus.FAILED:
    case TaskStatus.CANCELLED:
      return 'error';
    case TaskStatus.IN_PROGRESS:
      return 'running';
    default:
      return status;
  }
};

export function TaskCard({ task, onClick }: TaskCardProps) {
  const repoName = useMemo(() => {
    if (!task.repo_url) return 'Unknown Repo';
    return deriveRepoName(task.repo_url);
  }, [task.repo_url]);

  return (
    <div className={`glass-panel task-card ${onClick ? 'task-card-clickable' : ''}`} onClick={onClick}>
      <div className="card-header">
        <span className={`status-badge ${getStatusClass(task.status)}`}>
          {task.status.replace(/_/g, ' ')}
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
          <div className={`run-status ${getRunStatusClass(task.latest_run_status)}`}>
            {task.latest_run_status.replace(/_/g, ' ')}
          </div>
        )}
      </div>
    </div>
  );
}
