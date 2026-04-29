import React, { useMemo } from 'react';
import {
  Clock,
  Terminal,
  Github,
  GitBranch,
  RotateCcw,
  SlidersHorizontal,
} from 'lucide-react';
import { TaskSummarySnapshot, TaskStatus } from '../types/task';
import { api } from '../services/api';
import { TaskApprovalSection } from './TaskApprovalSection';

interface TaskCardProps {
  task: TaskSummarySnapshot;
  onClick?: () => void;
  onRefresh?: () => void;
  onReplayWithOverrides?: (taskId: string) => void;
  isSelected?: boolean;
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

export function TaskCard({
  task,
  onClick,
  onRefresh,
  onReplayWithOverrides,
  isSelected = false,
}: TaskCardProps) {
  const [isReplaying, setIsReplaying] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const repoName = useMemo(() => {
    if (!task.repo_url) return 'Unknown Repo';
    return deriveRepoName(task.repo_url);
  }, [task.repo_url]);


  const handleReplay = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (isReplaying) return;
    setIsReplaying(true);
    try {
      setError(null);
      await api.replayTask(task.task_id);
      onRefresh?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to replay task');
    } finally {
      setIsReplaying(false);
    }
  };

  const handleOpenReplayOverrides = (event: React.MouseEvent) => {
    event.stopPropagation();
    setError(null);
    onReplayWithOverrides?.(task.task_id);
  };

  const isTerminal = task.status === TaskStatus.COMPLETED ||
                     task.status === TaskStatus.FAILED ||
                     task.status === TaskStatus.CANCELLED;

  return (
    <div
      className={`glass-panel task-card ${onClick ? 'task-card-clickable' : ''} ${
        isSelected ? 'task-card-selected' : ''
      }`}
      onClick={onClick}
    >
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

        {error && task.approval_status !== 'pending' && (
          <div className="card-error-text">
            {error}
          </div>
        )}

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
          <div className="footer-actions">
            {isTerminal && (
              <>
                <button
                  className="btn-icon-sm btn-replay"
                  onClick={handleReplay}
                  disabled={isReplaying}
                  title="Replay task (unchanged)"
                >
                  <RotateCcw size={14} className={isReplaying ? 'spin' : ''} />
                </button>
                <button
                  className="btn-icon-sm btn-replay-overrides"
                  onClick={handleOpenReplayOverrides}
                  disabled={isReplaying}
                  title="Replay task with overrides"
                >
                  <SlidersHorizontal size={14} />
                </button>
              </>
            )}
            {task.latest_run_status && (
              <div className={`run-status ${getRunStatusClass(task.latest_run_status)}`}>
                {task.latest_run_status.replace(/_/g, ' ')}
              </div>
            )}
          </div>
        </div>

        <TaskApprovalSection task={task} onRefresh={onRefresh} />
    </div>
  );
}
