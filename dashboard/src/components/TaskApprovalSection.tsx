import React from 'react';
import { ShieldAlert, XCircle, CheckCircle2 } from 'lucide-react';
import { api } from '../services/api';
import { TaskSnapshot, TaskSummarySnapshot } from '../types/task';

interface TaskApprovalSectionProps {
  task: TaskSnapshot | TaskSummarySnapshot;
  onRefresh?: () => void;
  className?: string;
}

export function TaskApprovalSection({ task, onRefresh, className = "" }: TaskApprovalSectionProps) {
  const [isDeciding, setIsDeciding] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  if (task.approval_status !== 'pending') {
    return null;
  }

  const handleApproval = async (approved: boolean) => {
    if (isDeciding) return;
    setIsDeciding(true);
    try {
      setError(null);
      await api.decideTaskApproval(task.task_id, approved);
      onRefresh?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save approval decision');
    } finally {
      setIsDeciding(false);
    }
  };

  return (
    <div className={`approval-banner ${className}`} onClick={(e) => e.stopPropagation()}>
      <div className="approval-content">
        <ShieldAlert size={16} className="text-warning" />
        <div className="approval-text">
          <p className="approval-type">
            {task.approval_type?.replace(/_/g, ' ') || 'Approval Required'}
          </p>
          {task.latest_run_requested_permission && (
            <p className="permission-tag">
              Permission: <code>{task.latest_run_requested_permission}</code>
            </p>
          )}
          {task.approval_reason && (
            <p className="approval-reason truncate" title={task.approval_reason}>
              {task.approval_reason}
            </p>
          )}
          {error && (
            <p className="approval-error">
              {error}
            </p>
          )}
        </div>
      </div>
      <div className="approval-buttons">
        <button
          className="btn btn-sm btn-reject"
          onClick={() => handleApproval(false)}
          disabled={isDeciding}
        >
          <XCircle size={14} />
          <span>Reject</span>
        </button>
        <button
          className="btn btn-sm btn-approve"
          onClick={() => handleApproval(true)}
          disabled={isDeciding}
        >
          <CheckCircle2 size={14} />
          <span>Approve</span>
        </button>
      </div>
    </div>
  );
}
