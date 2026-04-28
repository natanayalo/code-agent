import React from 'react';
import { TaskSnapshot } from '../types/task';

interface TaskDetailPanelProps {
  task: TaskSnapshot | null;
  loading: boolean;
  error: unknown;
  onClose: () => void;
}

function formatLabel(value: string | null | undefined): string {
  return (value || '').replace(/_/g, ' ');
}

function listItemKey(items: string[], item: string, index: number): string {
  const duplicateCountBefore = items.slice(0, index).filter((value) => value === item).length;
  return `${item}-${duplicateCountBefore}`;
}

function renderStringList(title: string, items: string[] | undefined) {
  if (!items || items.length === 0) return null;
  return (
    <div className="task-detail-group">
      <h5>{title}</h5>
      <ul>
        {items.map((item, index) => (
          <li key={`${title}-${listItemKey(items, item, index)}`}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

export function TaskDetailPanel({ task, loading, error, onClose }: TaskDetailPanelProps) {
  if (!task && !loading && !error) {
    return null;
  }

  return (
    <aside className="glass-panel task-detail-panel">
      <div className="task-detail-header">
        <h3>Task Detail</h3>
        <button className="icon-button" onClick={onClose} aria-label="Close task detail">
          x
        </button>
      </div>

      {loading && <p className="task-detail-muted">Loading task detail...</p>}

      {error && (
        <p className="task-detail-error">
          {error instanceof Error ? error.message : 'Failed to load task detail.'}
        </p>
      )}

      {task && (
        <div className="task-detail-content">
          <h4>{task.task_text}</h4>
          <p className="task-detail-meta">
            Status: <strong>{formatLabel(task.status)}</strong>
          </p>

          {task.task_spec ? (
            <section className="task-detail-section">
              <h4>TaskSpec</h4>
              <div className="task-detail-grid">
                <p>
                  <strong>Goal:</strong> {task.task_spec.goal}
                </p>
                <p>
                  <strong>Risk:</strong> {formatLabel(task.task_spec.risk_level)}
                </p>
                <p>
                  <strong>Type:</strong> {formatLabel(task.task_spec.task_type)}
                </p>
                <p>
                  <strong>Delivery:</strong> {formatLabel(task.task_spec.delivery_mode)}
                </p>
              </div>
              {renderStringList('Acceptance Criteria', task.task_spec.acceptance_criteria)}
              {renderStringList('Clarification Questions', task.task_spec.clarification_questions)}
            </section>
          ) : (
            <section className="task-detail-section">
              <h4>TaskSpec</h4>
              <p className="task-detail-muted">No TaskSpec captured for this task.</p>
            </section>
          )}

          <section className="task-detail-section">
            <h4>Pending Interactions</h4>
            {task.pending_interactions && task.pending_interactions.length > 0 ? (
              <ul className="task-interactions-list">
                {task.pending_interactions.map((interaction) => (
                  <li key={interaction.interaction_id}>
                    <p>
                      <strong>{formatLabel(interaction.interaction_type)}:</strong>{' '}
                      {interaction.summary}
                    </p>
                    <p className="task-detail-muted">Status: {formatLabel(interaction.status)}</p>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="task-detail-muted">No pending interactions.</p>
            )}
          </section>
        </div>
      )}
    </aside>
  );
}
