import React from 'react';
import { TaskSnapshot } from '../types/task';

interface TaskDetailPanelProps {
  task: TaskSnapshot | null;
  loading: boolean;
  error: unknown;
  onClose: () => void;
}

function formatLabel(value: string | null | undefined): string {
  const normalized = (value || '').trim();
  if (!normalized) {
    return 'unknown';
  }
  return normalized.replace(/_/g, ' ');
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return 'Unknown time';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString();
}

function formatDuration(seconds: number | undefined): string | null {
  if (typeof seconds !== 'number' || Number.isNaN(seconds)) {
    return null;
  }
  if (seconds < 1) {
    return `${seconds.toFixed(2)}s`;
  }
  return `${seconds.toFixed(1)}s`;
}

function renderStringList(title: string, items: string[] | undefined) {
  if (!items || items.length === 0) return null;
  const duplicateCounts = new Map<string, number>();
  return (
    <div className="task-detail-group">
      <h5>{title}</h5>
      <ul>
        {items.map((item) => {
          const duplicateIndex = duplicateCounts.get(item) || 0;
          duplicateCounts.set(item, duplicateIndex + 1);
          return <li key={`${title}-${item}-${duplicateIndex}`}>{item}</li>;
        })}
      </ul>
    </div>
  );
}

function renderJsonBlock(value: unknown) {
  if (value == null) return null;
  try {
    return <pre className="task-detail-json">{JSON.stringify(value, null, 2)}</pre>;
  } catch {
    return <pre className="task-detail-json">{String(value)}</pre>;
  }
}

function artifactRows(task: TaskSnapshot) {
  const run = task.latest_run;
  if (!run) return [];
  if (Array.isArray(run.artifact_index) && run.artifact_index.length > 0) {
    return run.artifact_index.map((artifact, idx) => ({
      key: `${artifact.name || 'artifact'}-${artifact.uri || 'uri'}-${idx}`,
      name: artifact.name || 'artifact',
      type: artifact.artifact_type || 'unknown',
      uri: artifact.uri || '',
      metadata: artifact.artifact_metadata,
    }));
  }
  if (Array.isArray(run.artifacts) && run.artifacts.length > 0) {
    return run.artifacts.map((artifact, idx) => ({
      key: `${artifact.artifact_id}-${idx}`,
      name: artifact.name,
      type: artifact.artifact_type,
      uri: artifact.uri,
      metadata: artifact.artifact_metadata,
    }));
  }
  return [];
}

export function TaskDetailPanel({ task, loading, error, onClose }: TaskDetailPanelProps) {
  const run = task?.latest_run ?? null;
  const runCommands = run?.commands_run ?? [];
  const artifacts = React.useMemo(() => (task ? artifactRows(task) : []), [task]);
  const sortedTimeline = React.useMemo(() => {
    if (!task?.timeline) return [];
    return [...task.timeline].sort((a, b) => {
      const sequenceA = a.sequence_number ?? 0;
      const sequenceB = b.sequence_number ?? 0;
      if (sequenceA !== sequenceB) {
        return sequenceA - sequenceB;
      }

      const createdA = new Date(a.created_at).getTime();
      const createdB = new Date(b.created_at).getTime();
      const normalizedA = Number.isNaN(createdA) ? 0 : createdA;
      const normalizedB = Number.isNaN(createdB) ? 0 : createdB;
      if (normalizedA !== normalizedB) {
        return normalizedA - normalizedB;
      }

      return (a.event_type || '').localeCompare(b.event_type || '');
    });
  }, [task?.timeline]);

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

          <section className="task-detail-section">
            <h4>Timeline</h4>
            {sortedTimeline.length > 0 ? (
              <ol className="task-timeline-list">
                {sortedTimeline.map((event, idx) => (
                  <li key={`${event.created_at}-${event.sequence_number ?? idx}-${idx}`}>
                    <p>
                      <strong>{formatLabel(event.event_type)}</strong>
                      <span className="task-detail-muted task-inline-meta">
                        #{event.sequence_number ?? idx} · attempt {event.attempt_number ?? 0} ·{' '}
                        {formatTimestamp(event.created_at)}
                      </span>
                    </p>
                    {event.message ? <p>{event.message}</p> : null}
                    {renderJsonBlock(event.payload)}
                  </li>
                ))}
              </ol>
            ) : (
              <p className="task-detail-muted">No timeline events recorded yet.</p>
            )}
          </section>

          <section className="task-detail-section">
            <h4>Commands &amp; Logs</h4>
            {run ? (
              runCommands.length > 0 ? (
                <ol className="task-command-list">
                  {runCommands.map((command, idx) => {
                    const duration = formatDuration(command.duration_seconds);
                    return (
                      <li key={`${command.command || 'command'}-${idx}`}>
                        <p>
                          <strong>{command.command || `Command ${idx + 1}`}</strong>
                        </p>
                        <p className="task-detail-muted">
                          Exit: {command.exit_code ?? 'unknown'}
                          {command.timed_out ? ' · timed out' : ''}
                          {duration ? ` · ${duration}` : ''}
                        </p>
                        {(command.stdout_artifact_uri || command.stderr_artifact_uri) && (
                          <ul className="task-command-artifacts">
                            {command.stdout_artifact_uri && (
                              <li>
                                stdout: <code>{command.stdout_artifact_uri}</code>
                              </li>
                            )}
                            {command.stderr_artifact_uri && (
                              <li>
                                stderr: <code>{command.stderr_artifact_uri}</code>
                              </li>
                            )}
                          </ul>
                        )}
                      </li>
                    );
                  })}
                </ol>
              ) : (
                <p className="task-detail-muted">No commands captured for the latest run.</p>
              )
            ) : (
              <p className="task-detail-muted">No run metadata available yet.</p>
            )}
          </section>

          <section className="task-detail-section">
            <h4>Artifacts</h4>
            {artifacts.length > 0 ? (
              <ul className="task-artifact-list">
                {artifacts.map((artifact) => (
                  <li key={artifact.key}>
                    <p>
                      <strong>{artifact.name}</strong>
                      <span className="task-detail-muted task-inline-meta">
                        {formatLabel(artifact.type)}
                      </span>
                    </p>
                    <p>
                      <code>{artifact.uri || 'No URI'}</code>
                    </p>
                    {renderJsonBlock(artifact.metadata)}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="task-detail-muted">No artifacts persisted for the latest run.</p>
            )}
          </section>
        </div>
      )}
    </aside>
  );
}
