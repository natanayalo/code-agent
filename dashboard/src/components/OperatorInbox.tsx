import React, { useState } from 'react';
import {
  ExternalLink,
  RotateCcw,
  GitPullRequest,
  AlertCircle,
  CheckCircle2,
  Clock,
} from 'lucide-react';
import { InteractionInboxCard, TaskSummarySnapshot, TaskStatus } from '../types/task';
import { api } from '../services/api';
import { isValidHttpUrl } from '../utils/formatters';

export interface OperatorInboxProps {
  interactions: InteractionInboxCard[];
  selectedTaskId: string | null;
  onOpenTask: (taskId: string) => void;
  tasks?: TaskSummarySnapshot[];
  onRefresh?: () => void;
}

const getDeliveryActionPriority = (task: TaskSummarySnapshot): number => {
  const meta = task.delivery_metadata;
  if (!meta) return 99;
  if (meta.ci_status === 'failed') return 1;
  if (meta.ci_status === 'pending') return 2;
  if (meta.pr_state === 'open' || meta.pr_draft) return 3;
  return 4;
};

const getSafeNextAction = (meta: TaskSummarySnapshot['delivery_metadata']) => {
  if (!meta) return 'No delivery action required';
  if (meta.ci_status === 'failed') {
    const failedJobs =
      Array.isArray(meta.ci_failed_jobs) && meta.ci_failed_jobs.length > 0
        ? meta.ci_failed_jobs.join(', ')
        : 'checks';
    return `CI failed (${failedJobs}) — inspect check logs or replay task`;
  }
  if (meta.ci_status === 'pending') {
    return 'CI checks in progress — monitor draft PR checks';
  }
  if (meta.ci_status === 'passed') {
    if (meta.pr_draft) {
      return 'CI passed — review draft PR on GitHub when ready';
    }
    return 'CI passed — ready for operator review on GitHub';
  }
  if (meta.pr_state === 'merged') {
    return 'Pull request merged';
  }
  return 'Review pull request or check status on GitHub';
};

const getCiBadgeClass = (ciStatus: string | null | undefined): string => {
  switch (ciStatus) {
    case 'passed':
      return 'ci-badge ci-passed';
    case 'failed':
      return 'ci-badge ci-failed';
    case 'pending':
      return 'ci-badge ci-pending';
    default:
      return 'ci-badge ci-unknown';
  }
};

export function OperatorInbox({
  interactions,
  selectedTaskId,
  onOpenTask,
  tasks = [],
  onRefresh,
}: OperatorInboxProps) {
  const [replayingId, setReplayingId] = useState<string | null>(null);
  const [replayError, setReplayError] = useState<{ [taskId: string]: string }>({});

  const deliveryTasks = React.useMemo(() => {
    return tasks
      .filter((t) => {
        const m = t.delivery_metadata;
        return Boolean(
          m &&
            (m.pr_url ||
              m.pr_number != null ||
              m.ci_status ||
              m.source_issue_number != null ||
              (m.check_runs && m.check_runs.length > 0))
        );
      })
      .sort((a, b) => getDeliveryActionPriority(a) - getDeliveryActionPriority(b));
  }, [tasks]);

  const handleReplay = async (taskId: string) => {
    if (replayingId) return;
    setReplayingId(taskId);
    setReplayError((prev) => ({ ...prev, [taskId]: '' }));
    try {
      await api.replayTask(taskId);
      onRefresh?.();
    } catch (err) {
      setReplayError((prev) => ({
        ...prev,
        [taskId]: err instanceof Error ? err.message : 'Failed to replay task',
      }));
    } finally {
      setReplayingId(null);
    }
  };

  const totalCount = interactions.length + deliveryTasks.length;

  return (
    <section className="glass-panel operator-inbox">
      <div className="operator-inbox-header">
        <h3>Operator Inbox</h3>
        <span>
          {interactions.length} pending interaction{interactions.length === 1 ? '' : 's'}
          {deliveryTasks.length > 0 && ` · ${deliveryTasks.length} PR/CI item${deliveryTasks.length === 1 ? '' : 's'}`}
        </span>
      </div>

      {totalCount === 0 ? (
        <p className="task-detail-muted">No pending interactions.</p>
      ) : (
        <div className="operator-inbox-sections">
          {interactions.length > 0 && (
            <div className="operator-inbox-group">
              <div className="operator-inbox-subheading">
                <Clock size={14} />
                <span>Pending Interactions ({interactions.length})</span>
              </div>
              <ul className="operator-inbox-list">
                {interactions.map((card) => (
                  <li key={card.interaction.interaction_id}>
                    <button
                      className={`operator-inbox-item ${
                        selectedTaskId === card.task_id ? 'selected' : ''
                      }`}
                      onClick={() => onOpenTask(card.task_id)}
                    >
                      <span className="operator-inbox-item-content">
                        <span className="operator-inbox-text truncate" title={card.task_text}>
                          {card.task_text}
                        </span>
                        <span
                          className="operator-inbox-summary truncate"
                          title={card.interaction.summary}
                        >
                          {card.interaction.summary}
                        </span>
                      </span>
                      <span className="operator-inbox-type badge">
                        {card.interaction.interaction_type}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {deliveryTasks.length > 0 && (
            <div className="operator-inbox-group">
              <div className="operator-inbox-subheading">
                <GitPullRequest size={14} />
                <span>Pull Request & CI Delivery ({deliveryTasks.length})</span>
              </div>
              <ul className="operator-inbox-delivery-list">
                {deliveryTasks.map((task) => {
                  const meta = task.delivery_metadata!;
                  const isSelected = selectedTaskId === task.task_id;
                  const isTerminal =
                    task.status === TaskStatus.COMPLETED ||
                    task.status === TaskStatus.FAILED ||
                    task.status === TaskStatus.CANCELLED;
                  const failedCheck = meta.check_runs?.find(
                    (c) =>
                      c.conclusion === 'failure' ||
                      c.conclusion === 'timed_out' ||
                      c.conclusion === 'cancelled'
                  );

                  return (
                    <li
                      key={task.task_id}
                      className={`operator-inbox-delivery-card ${isSelected ? 'selected' : ''}`}
                    >
                      <div className="operator-inbox-delivery-header">
                        <span className="operator-inbox-text truncate" title={task.task_text}>
                          {task.task_text}
                        </span>
                        <div className="operator-inbox-badges">
                          {meta.pr_number != null && (
                            <span
                              className={`pr-badge ${meta.pr_draft ? 'pr-draft' : meta.pr_state === 'merged' ? 'pr-merged' : 'pr-open'}`}
                            >
                              <GitPullRequest size={12} />
                              {meta.pr_draft
                                ? `Draft PR #${meta.pr_number}`
                                : `PR #${meta.pr_number}${meta.pr_state ? ` (${meta.pr_state})` : ''}`}
                            </span>
                          )}
                          {meta.ci_status && (
                            <span className={getCiBadgeClass(meta.ci_status)}>
                              {meta.ci_status === 'passed' && <CheckCircle2 size={12} />}
                              {meta.ci_status === 'failed' && <AlertCircle size={12} />}
                              {meta.ci_status === 'pending' && <Clock size={12} />}
                              CI: {meta.ci_status}
                            </span>
                          )}
                        </div>
                      </div>

                      <div className="operator-inbox-next-action">
                        {getSafeNextAction(meta)}
                      </div>

                      {replayError[task.task_id] && (
                        <div className="card-error-text">
                          {replayError[task.task_id]}
                        </div>
                      )}

                      <div className="operator-inbox-actions">
                        <div className="operator-inbox-links">
                          {meta.source_issue_url && isValidHttpUrl(meta.source_issue_url) && (
                            <a
                              href={meta.source_issue_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="btn-link"
                            >
                              Issue #{meta.source_issue_number ?? ''}
                              <ExternalLink size={12} />
                            </a>
                          )}
                          {meta.pr_url && isValidHttpUrl(meta.pr_url) && (
                            <a
                              href={meta.pr_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="btn-link"
                            >
                              PR #{meta.pr_number ?? ''}
                              <ExternalLink size={12} />
                            </a>
                          )}
                          {failedCheck?.html_url && isValidHttpUrl(failedCheck.html_url) && (
                            <a
                              href={failedCheck.html_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="btn-link text-error"
                            >
                              View Check ({failedCheck.name})
                              <ExternalLink size={12} />
                            </a>
                          )}
                        </div>

                        <div className="operator-inbox-buttons">
                          <button
                            type="button"
                            onClick={() => onOpenTask(task.task_id)}
                            className="btn-secondary-sm"
                          >
                            Inspect Task
                          </button>
                          {isTerminal && (meta.ci_status === 'failed' || task.status === TaskStatus.FAILED) && (
                            <button
                              type="button"
                              onClick={() => handleReplay(task.task_id)}
                              disabled={replayingId === task.task_id}
                              className="btn-secondary-sm btn-replay"
                            >
                              <RotateCcw
                                size={12}
                                className={replayingId === task.task_id ? 'spin' : ''}
                              />
                              Replay Task
                            </button>
                          )}
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
