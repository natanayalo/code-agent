import React from 'react';
import { MessageSquare, Send, AlertCircle } from 'lucide-react';
import { api } from '../services/api';
import { HumanInteractionSnapshot, TaskSnapshot } from '../types/task';

interface TaskInteractionSectionProps {
  task: TaskSnapshot;
  interaction: HumanInteractionSnapshot;
  onRefresh?: () => void;
  className?: string;
}

export function TaskInteractionSection({
  task,
  interaction,
  onRefresh,
  className = '',
}: TaskInteractionSectionProps) {
  const [responseText, setResponseText] = React.useState('');
  const [isSubmitting, setIsSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  if (interaction.status !== 'pending') {
    return null;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isSubmitting || !responseText.trim()) return;

    setIsSubmitting(true);
    try {
      setError(null);
      await api.respondToInteraction(task.task_id, interaction.interaction_id, 'resolved', {
        text: responseText.trim(),
      });
      setResponseText('');
      onRefresh?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to submit response');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className={`interaction-response-card ${className}`}>
      <div className="interaction-header">
        <MessageSquare size={16} className="text-accent" />
        <span className="interaction-type-label">Clarification Required</span>
      </div>

      <p className="interaction-summary">{interaction.summary}</p>

      {interaction.data?.questions && Array.isArray(interaction.data.questions) && (
        <ul className="interaction-questions">
          {interaction.data.questions.map((q: string, idx: number) => (
            <li key={idx}>{q}</li>
          ))}
        </ul>
      )}

      <form onSubmit={handleSubmit} className="interaction-form">
        <textarea
          className="interaction-textarea"
          placeholder="Type your response here..."
          value={responseText}
          onChange={(e) => setResponseText(e.target.value)}
          disabled={isSubmitting}
          required
        />

        {error && (
          <div className="interaction-error">
            <AlertCircle size={14} />
            <span>{error}</span>
          </div>
        )}

        <button
          type="submit"
          className="btn btn-sm btn-approve"
          disabled={isSubmitting || !responseText.trim()}
        >
          <Send size={14} />
          <span>{isSubmitting ? 'Sending...' : 'Send Response'}</span>
        </button>
      </form>
    </div>
  );
}
