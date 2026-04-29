import React from 'react';
import { RotateCcw, X } from 'lucide-react';
import { api } from '../services/api';
import { TaskReplayRequest, WORKER_OPTIONS, WorkerType } from '../types/task';

interface ReplayWithOverridesModalProps {
  taskId: string;
  isOpen: boolean;
  onClose: () => void;
  onReplaySuccess?: () => void;
}

interface JsonParseResult {
  parsed?: Record<string, unknown>;
  error?: string;
}

function parseOptionalJsonObject(fieldName: string, input: string): JsonParseResult {
  const trimmed = input.trim();
  if (!trimmed) {
    return {};
  }

  try {
    const parsed = JSON.parse(trimmed);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
      return { error: `${fieldName} must be a JSON object.` };
    }
    return { parsed: parsed as Record<string, unknown> };
  } catch {
    return { error: `${fieldName} must be valid JSON.` };
  }
}

export function ReplayWithOverridesModal({
  taskId,
  isOpen,
  onClose,
  onReplaySuccess,
}: ReplayWithOverridesModalProps) {
  const [workerOverride, setWorkerOverride] = React.useState<'' | WorkerType>('');
  const [constraintsJson, setConstraintsJson] = React.useState('');
  const [budgetJson, setBudgetJson] = React.useState('');
  const [secretsJson, setSecretsJson] = React.useState('');
  const [isSubmitting, setIsSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = React.useState<{
    constraints?: string;
    budget?: string;
    secrets?: string;
  }>({});

  React.useEffect(() => {
    if (!isOpen) {
      setWorkerOverride('');
      setConstraintsJson('');
      setBudgetJson('');
      setSecretsJson('');
      setError(null);
      setFieldErrors({});
      setIsSubmitting(false);
    }
  }, [isOpen]);

  if (!isOpen) {
    return null;
  }

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isSubmitting) {
      return;
    }

    setError(null);
    setFieldErrors({});

    const constraintsResult = parseOptionalJsonObject('Constraints override', constraintsJson);
    const budgetResult = parseOptionalJsonObject('Budget override', budgetJson);
    const secretsResult = parseOptionalJsonObject('Secrets override', secretsJson);

    const nextFieldErrors: { constraints?: string; budget?: string; secrets?: string } = {};
    if (constraintsResult.error) {
      nextFieldErrors.constraints = constraintsResult.error;
    }
    if (budgetResult.error) {
      nextFieldErrors.budget = budgetResult.error;
    }
    if (secretsResult.error) {
      nextFieldErrors.secrets = secretsResult.error;
    }

    if (Object.keys(nextFieldErrors).length > 0) {
      setFieldErrors(nextFieldErrors);
      return;
    }

    const replayRequest: TaskReplayRequest = {};

    if (workerOverride) {
      replayRequest.worker_override = workerOverride as WorkerType;
    }
    if (constraintsJson.trim()) {
      replayRequest.constraints = constraintsResult.parsed;
    }
    if (budgetJson.trim()) {
      replayRequest.budget = budgetResult.parsed;
    }
    if (secretsJson.trim() && secretsResult.parsed) {
      const invalidKey = Object.entries(secretsResult.parsed).find(
        ([, value]) => typeof value !== 'string'
      )?.[0];
      if (invalidKey) {
        setFieldErrors({
          secrets: `Secrets override values must be strings (invalid key: ${invalidKey}).`,
        });
        return;
      }
      replayRequest.secrets = secretsResult.parsed as Record<string, string>;
    }

    setIsSubmitting(true);
    try {
      await api.replayTask(
        taskId,
        Object.keys(replayRequest).length > 0 ? replayRequest : undefined
      );
      onReplaySuccess?.();
      onClose();
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : 'Failed to replay task');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="modal-overlay">
      <div
        className="glass-panel replay-overrides-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="replay-overrides-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="replay-overrides-header">
          <h4 id="replay-overrides-title">Replay With Overrides</h4>
          <button
            type="button"
            className="icon-button"
            onClick={onClose}
            aria-label="Close replay override modal"
            disabled={isSubmitting}
          >
            <X size={18} />
          </button>
        </div>

        <p className="task-detail-muted">
          Override worker, constraints, budget, or secrets for this replay. Leave fields blank to
          keep the original task settings.
        </p>

        <form className="replay-overrides-form" onSubmit={handleSubmit}>
          <label htmlFor="replay-worker-override">Worker Override</label>
          <select
            id="replay-worker-override"
            className="replay-overrides-select"
            value={workerOverride}
            onChange={(event) => setWorkerOverride(event.target.value as '' | WorkerType)}
          >
            <option value="">Original worker selection</option>
            {WORKER_OPTIONS.map((worker, index) => (
              <option key={`${worker}-${index}`} value={worker}>
                {worker}
              </option>
            ))}
          </select>

          <label htmlFor="replay-constraints-json">Constraints Override (JSON object)</label>
          <textarea
            id="replay-constraints-json"
            className="replay-overrides-textarea"
            value={constraintsJson}
            onChange={(event) => setConstraintsJson(event.target.value)}
            placeholder={'{\n  "max_files": 10\n}'}
            rows={5}
          />
          {fieldErrors.constraints ? (
            <p className="task-detail-error replay-overrides-error">{fieldErrors.constraints}</p>
          ) : null}

          <label htmlFor="replay-budget-json">Budget Override (JSON object)</label>
          <textarea
            id="replay-budget-json"
            className="replay-overrides-textarea"
            value={budgetJson}
            onChange={(event) => setBudgetJson(event.target.value)}
            placeholder={'{\n  "max_steps": 20\n}'}
            rows={5}
          />
          {fieldErrors.budget ? (
            <p className="task-detail-error replay-overrides-error">{fieldErrors.budget}</p>
          ) : null}

          <label htmlFor="replay-secrets-json">Secrets Override (JSON object)</label>
          <textarea
            id="replay-secrets-json"
            className="replay-overrides-textarea"
            value={secretsJson}
            onChange={(event) => setSecretsJson(event.target.value)}
            placeholder={'{\n  "API_TOKEN": "new-value"\n}'}
            rows={5}
          />
          {fieldErrors.secrets ? (
            <p className="task-detail-error replay-overrides-error">{fieldErrors.secrets}</p>
          ) : null}

          {error ? <p className="task-detail-error replay-overrides-error">{error}</p> : null}

          <div className="replay-overrides-actions">
            <button type="button" className="btn btn-reject" onClick={onClose} disabled={isSubmitting}>
              Cancel
            </button>
            <button type="submit" className="btn btn-approve" disabled={isSubmitting}>
              <RotateCcw size={14} className={isSubmitting ? 'spin' : ''} />
              <span>Replay Task</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
