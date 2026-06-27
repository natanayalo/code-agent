import React from 'react';
import { InteractionInboxCard } from '../types/task';

interface OperatorInboxProps {
  interactions: InteractionInboxCard[];
  selectedTaskId: string | null;
  onOpenTask: (taskId: string) => void;
}

export function OperatorInbox({ interactions, selectedTaskId, onOpenTask }: OperatorInboxProps) {
  return (
    <section className="glass-panel operator-inbox">
      <div className="operator-inbox-header">
        <h3>Operator Inbox</h3>
        <span>{interactions.length} pending interactions</span>
      </div>
      {interactions.length === 0 ? (
        <p className="task-detail-muted">No pending interactions.</p>
      ) : (
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
                  <span className="operator-inbox-summary truncate" title={card.interaction.summary}>
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
      )}
    </section>
  );
}
