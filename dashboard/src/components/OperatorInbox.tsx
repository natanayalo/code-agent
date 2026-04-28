import React from 'react';
import { TaskSummarySnapshot } from '../types/task';

interface OperatorInboxProps {
  tasks: TaskSummarySnapshot[];
  selectedTaskId: string | null;
  onOpenTask: (taskId: string) => void;
}

export function OperatorInbox({ tasks, selectedTaskId, onOpenTask }: OperatorInboxProps) {
  return (
    <section className="glass-panel operator-inbox">
      <div className="operator-inbox-header">
        <h3>Operator Inbox</h3>
        <span>{tasks.length} tasks need input</span>
      </div>
      {tasks.length === 0 ? (
        <p className="task-detail-muted">No pending interactions.</p>
      ) : (
        <ul className="operator-inbox-list">
          {tasks.map((task) => (
            <li key={task.task_id}>
              <button
                className={`operator-inbox-item ${
                  selectedTaskId === task.task_id ? 'selected' : ''
                }`}
                onClick={() => onOpenTask(task.task_id)}
              >
                <span className="operator-inbox-text truncate" title={task.task_text}>
                  {task.task_text}
                </span>
                <span className="operator-inbox-count">
                  {task.pending_interaction_count || 0} pending
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
