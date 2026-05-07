import React from 'react';
import { X } from 'lucide-react';
import { TaskSnapshot, VerifierOutcomeItem, VerifierOutcomeSnapshot } from '../types/task';
import { TaskApprovalSection } from './TaskApprovalSection';
import { formatLabel } from '../utils/formatters';
import { api } from '../services/api';

interface TaskDetailPanelProps {
  task: TaskSnapshot | null;
  loading: boolean;
  error: unknown;
  onClose: () => void;
  onRefresh?: () => void;
}

interface TraceLink {
  provider: string;
  url: string;
}

interface SpanStatusCount {
  status: string;
  count: number;
}

interface TraceObservabilitySnapshot {
  traceIds: string[];
  providerLinks: TraceLink[];
  spanStatusCounts: SpanStatusCount[];
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
  // Deduplicate items to ensure unique React keys as per review feedback
  const uniqueItems = Array.from(new Set(items));
  return (
    <div className="task-detail-group">
      <h5>{title}</h5>
      <ul>
        {uniqueItems.map((item) => (
          <li key={`${title}-${item}`}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function renderJsonBlock(value: unknown) {
  if (value == null) return null;
  try {
    return <pre className="task-detail-json">{JSON.stringify(value, null, 2)}</pre>;
  } catch {
    const valueType =
      typeof value === 'object' && value !== null && value.constructor?.name
        ? value.constructor.name
        : typeof value;
    return <pre className="task-detail-json">{`Unserializable ${valueType} value`}</pre>;
  }
}

function artifactRows(run: TaskSnapshot['latest_run']) {
  if (!run) return [];
  if (Array.isArray(run.artifact_index) && run.artifact_index.length > 0) {
    return run.artifact_index.map((artifact) => ({
      key: artifact.id || artifact.uri || artifact.name || 'artifact',
      name: artifact.name || 'artifact',
      type: artifact.artifact_type || 'unknown',
      uri: artifact.uri || '',
      metadata: artifact.artifact_metadata,
    }));
  }
  if (Array.isArray(run.artifacts) && run.artifacts.length > 0) {
    return run.artifacts.map((artifact) => ({
      key: artifact.artifact_id,
      name: artifact.name,
      type: artifact.artifact_type,
      uri: artifact.uri,
      metadata: artifact.artifact_metadata,
    }));
  }
  return [];
}

function normalizeToken(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, '');
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function extractVerifierOutcome(value: unknown): VerifierOutcomeSnapshot {
  const payload = asRecord(value);
  if (!payload) {
    return { status: null, summary: null, items: [] };
  }

  const status = typeof payload.status === 'string' ? payload.status : null;
  const summary = typeof payload.summary === 'string' ? payload.summary : null;
  const items = Array.isArray(payload.items)
    ? payload.items
        .map((entry) => {
          const item = asRecord(entry);
          if (!item) return null;
          const label = typeof item.label === 'string' ? item.label : null;
          const itemStatus = typeof item.status === 'string' ? item.status : null;
          const message = typeof item.message === 'string' ? item.message : null;
          if (!label || !itemStatus) return null;
          // Generate a stable ID if not provided by backend
          const generatedId = label + '-' + itemStatus + '-' + (message || '');
          return {
            id: typeof item.id === 'string' ? item.id : generatedId,
            label,
            status: itemStatus,
            message,
          };
        })
        .filter((item): item is VerifierOutcomeItem => item !== null)
    : [];

  return {
    status,
    summary,
    items,
  };
}

function computeRunDuration(startedAt: string | null | undefined, finishedAt: string | null | undefined) {
  if (!startedAt || !finishedAt) return null;
  const startedTimestamp = new Date(startedAt).getTime();
  const finishedTimestamp = new Date(finishedAt).getTime();
  if (Number.isNaN(startedTimestamp) || Number.isNaN(finishedTimestamp)) {
    return null;
  }
  const elapsedSeconds = Math.max(0, (finishedTimestamp - startedTimestamp) / 1000);
  return formatDuration(elapsedSeconds);
}

function isTerminalTaskStatus(status: string | null | undefined): boolean {
  return status === 'completed' || status === 'failed' || status === 'cancelled';
}

function parseHttpUrl(value: string): URL | null {
  try {
    const parsed = new URL(value);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return parsed;
    }
    return null;
  } catch {
    return null;
  }
}

function hostMatchesDomain(host: string, domain: string): boolean {
  return host === domain || host.endsWith(`.${domain}`);
}

function inferTraceProvider(url: URL): string {
  const host = url.hostname.toLowerCase();
  if (hostMatchesDomain(host, 'smith.langchain.com')) return 'LangSmith';
  if (hostMatchesDomain(host, 'langfuse.com')) return 'Langfuse';
  if (hostMatchesDomain(host, 'arize.com')) return 'Phoenix';
  return host;
}

function isTraceIdKey(normalizedKey: string): boolean {
  return (
    normalizedKey === 'traceid' ||
    normalizedKey.endsWith('traceid') ||
    (normalizedKey.includes('trace') && normalizedKey.includes('id'))
  );
}

function isTraceUrlKey(normalizedKey: string): boolean {
  return (
    normalizedKey.includes('trace') ||
    normalizedKey.includes('span') ||
    normalizedKey.includes('otel') ||
    normalizedKey.includes('langsmith') ||
    normalizedKey.includes('langfuse') ||
    normalizedKey.includes('phoenix') ||
    normalizedKey === 'runurl'
  );
}

function isSpanSummaryKey(normalizedKey: string): boolean {
  return (
    normalizedKey.includes('spanstatus') ||
    normalizedKey.includes('spansbystatus') ||
    normalizedKey.includes('spanstatuscounts') ||
    normalizedKey.includes('spancounts')
  );
}

function isSpansArrayKey(normalizedKey: string): boolean {
  return normalizedKey === 'spans' || normalizedKey.endsWith('spans');
}

function extractTraceObservability(task: TaskSnapshot | null): TraceObservabilitySnapshot {
  const traceIds = new Set<string>();
  const providerLinks = new Map<string, TraceLink>();
  const spanStatusCounts = new Map<string, number>();
  const visited = new WeakSet<object>();

  const addTraceId = (value: string) => {
    const trimmed = value.trim();
    if (trimmed) {
      traceIds.add(trimmed);
    }
  };

  const addProviderLink = (value: string) => {
    const trimmed = value.trim();
    if (!trimmed) return;
    const parsed = parseHttpUrl(trimmed);
    if (!parsed) return;
    providerLinks.set(parsed.toString(), {
      provider: inferTraceProvider(parsed),
      url: parsed.toString(),
    });
  };

  if (task?.trace_id) {
    addTraceId(task.trace_id);
  }
  if (task?.trace_url) {
    addProviderLink(task.trace_url);
  }

  const addSpanCount = (status: string, count: number) => {
    if (!Number.isFinite(count) || count < 0) return;
    const normalizedStatus = status.trim();
    if (!normalizedStatus) return;
    spanStatusCounts.set(normalizedStatus, (spanStatusCounts.get(normalizedStatus) || 0) + count);
  };

  const extractSpanStatusMap = (candidate: Record<string, unknown>) => {
    for (const [status, rawCount] of Object.entries(candidate)) {
      if (typeof rawCount === 'number') {
        addSpanCount(status, rawCount);
      }
    }
  };

  const parseTraceParent = (traceParentValue: string) => {
    const match = traceParentValue
      .trim()
      .match(/^[\da-f]{2}-([\da-f]{32})-[\da-f]{16}-[\da-f]{2}$/i);
    if (match) {
      addTraceId(match[1]);
    }
  };

  const visit = (value: unknown, key = '', depth = 0) => {
    if (depth > 6 || value == null) return;
    const normalizedKey = normalizeToken(key);

    if (Array.isArray(value)) {
      value.forEach((item) => visit(item, key, depth + 1));
      return;
    }

    if (typeof value === 'string') {
      if (isTraceIdKey(normalizedKey)) {
        addTraceId(value);
      }
      if (normalizedKey === 'traceparent') {
        parseTraceParent(value);
      }
      if (isTraceUrlKey(normalizedKey)) {
        addProviderLink(value);
      }
      return;
    }

    const objectValue = asRecord(value);
    if (!objectValue) return;
    if (visited.has(objectValue)) return;
    visited.add(objectValue);

    if (isSpanSummaryKey(normalizedKey)) {
      extractSpanStatusMap(objectValue);
    }

    for (const [entryKey, entryValue] of Object.entries(objectValue)) {
      const entryKeyNormalized = normalizeToken(entryKey);
      if (typeof entryValue === 'number' && isSpanSummaryKey(entryKeyNormalized)) {
        addSpanCount(entryKey, entryValue);
      }
      if (isSpansArrayKey(entryKeyNormalized) && Array.isArray(entryValue)) {
        for (const span of entryValue) {
          const spanRecord = asRecord(span);
          const statusValue = spanRecord?.status;
          if (typeof statusValue === 'string') {
            addSpanCount(statusValue, 1);
          }
        }
      }
      visit(entryValue, entryKey, depth + 1);
    }
  };

  const sources: unknown[] = [];
  if (task?.latest_run) {
    sources.push(task.latest_run.budget_usage, task.latest_run.verifier_outcome);
    task.latest_run.artifact_index?.forEach((artifact) => {
      sources.push(artifact);
    });
    task.latest_run.artifacts?.forEach((artifact) => {
      sources.push(artifact);
    });
  }
  task?.timeline?.forEach((event) => {
    sources.push(event.payload);
  });

  sources.forEach((source) => visit(source));

  const sortedTraceIds = [...traceIds].sort((a, b) => a.localeCompare(b));
  const sortedLinks = [...providerLinks.values()].sort((a, b) =>
    `${a.provider}:${a.url}`.localeCompare(`${b.provider}:${b.url}`)
  );
  const sortedSpanStatusCounts = [...spanStatusCounts.entries()]
    .map(([status, count]) => ({ status, count }))
    .sort((a, b) => b.count - a.count || a.status.localeCompare(b.status));

  return {
    traceIds: sortedTraceIds,
    providerLinks: sortedLinks,
    spanStatusCounts: sortedSpanStatusCounts,
  };
}

export function TaskDetailPanel({ task, loading, error, onClose, onRefresh }: TaskDetailPanelProps) {
  const [isCancelling, setIsCancelling] = React.useState(false);
  const [cancelError, setCancelError] = React.useState<string | null>(null);
  const [interactionError, setInteractionError] = React.useState<string | null>(null);
  const [resolvingInteractionId, setResolvingInteractionId] = React.useState<string | null>(null);
  const run = task?.latest_run ?? null;
  const runCommands = React.useMemo(() => run?.commands_run ?? [], [run]);
  const changedFiles = React.useMemo(() => run?.files_changed ?? [], [run]);
  const runDuration = React.useMemo(
    () => computeRunDuration(run?.started_at, run?.finished_at),
    [run?.started_at, run?.finished_at]
  );
  const verifierOutcome = React.useMemo(
    () => extractVerifierOutcome(run?.verifier_outcome),
    [run?.verifier_outcome]
  );
  const artifacts = React.useMemo(() => artifactRows(run), [run]);
  const traceObservability = React.useMemo(() => extractTraceObservability(task), [task]);
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
  const hasTraceObservability =
    traceObservability.traceIds.length > 0 ||
    traceObservability.providerLinks.length > 0 ||
    traceObservability.spanStatusCounts.length > 0;

  if (!task && !loading && !error) {
    return null;
  }

  const hasPendingInteractions = Boolean(task?.pending_interactions && task.pending_interactions.length > 0);
  const isTaskTerminal = isTerminalTaskStatus(task?.status);

  const handleCancelTask = async () => {
    if (!task || isCancelling || isTaskTerminal) return;
    setCancelError(null);
    setIsCancelling(true);
    try {
      await api.cancelTask(task.task_id);
      onRefresh?.();
    } catch (cancelTaskError) {
      setCancelError(cancelTaskError instanceof Error ? cancelTaskError.message : 'Failed to cancel task.');
    } finally {
      setIsCancelling(false);
    }
  };

  const handleResolveInteraction = async (interactionId: string) => {
    if (!task || resolvingInteractionId) return;
    setInteractionError(null);
    setResolvingInteractionId(interactionId);
    try {
      await api.recordInteractionResponse(task.task_id, interactionId, {
        status: 'resolved',
        response_data: {
          source: 'dashboard_operator',
          action: 'resolve',
          timestamp: new Date().toISOString(),
        },
      });
      onRefresh?.();
    } catch (resolveError) {
      setInteractionError(
        resolveError instanceof Error ? resolveError.message : 'Failed to resolve interaction.'
      );
    } finally {
      setResolvingInteractionId(null);
    }
  };

  return (
    <aside className="glass-panel task-detail-panel">
      <div className="task-detail-header">
        <h3>Task Detail</h3>
        <button
          onClick={onClose}
          className="icon-button"
          title="Close Panel"
          aria-label="Close task detail"
        >
          <X size={20} />
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
          {!isTaskTerminal ? (
            <div className="task-detail-actions">
              <button
                type="button"
                className="btn btn-reject"
                onClick={handleCancelTask}
                disabled={isCancelling}
              >
                {isCancelling ? 'Cancelling...' : 'Cancel Task'}
              </button>
            </div>
          ) : null}
          {cancelError ? <p className="task-detail-error">{cancelError}</p> : null}

          <TaskApprovalSection task={task} onRefresh={onRefresh} className="task-detail-approval" />

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
                    <div className="task-interaction-actions">
                      <button
                        type="button"
                        className="btn btn-approve"
                        onClick={() => handleResolveInteraction(interaction.interaction_id)}
                        disabled={resolvingInteractionId !== null}
                      >
                        {resolvingInteractionId === interaction.interaction_id
                          ? 'Resolving...'
                          : 'Resolve'}
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="task-detail-muted">No pending interactions.</p>
            )}
            {hasPendingInteractions && interactionError ? (
              <p className="task-detail-error">{interactionError}</p>
            ) : null}
          </section>

          <section className="task-detail-section">
            <h4>Run Observability</h4>
            {run ? (
              <>
                <div className="task-detail-grid">
                  <p>
                    <strong>Worker:</strong> {run.worker_type || task.chosen_worker || 'unknown'}
                  </p>
                  <p>
                    <strong>Profile:</strong> {run.worker_profile || task.chosen_profile || 'n/a'}
                  </p>
                  <p>
                    <strong>Runtime Mode:</strong>{' '}
                    {formatLabel(run.runtime_mode || task.runtime_mode || 'n/a')}
                  </p>
                  <p>
                    <strong>Workspace:</strong> {run.workspace_id || 'n/a'}
                  </p>
                  <p>
                    <strong>Started:</strong> {formatTimestamp(run.started_at)}
                  </p>
                  <p>
                    <strong>Finished:</strong>{' '}
                    {run.finished_at ? formatTimestamp(run.finished_at) : 'In progress'}
                  </p>
                  <p>
                    <strong>Duration:</strong> {runDuration || 'Unknown duration'}
                  </p>
                  <p>
                    <strong>Status:</strong> {formatLabel(run.status)}
                  </p>
                </div>

                {changedFiles.length > 0 ? (
                  renderStringList('Changed Files', changedFiles)
                ) : (
                  <p className="task-detail-muted">No changed files captured for the latest run.</p>
                )}

                {verifierOutcome.status || verifierOutcome.summary || verifierOutcome.items.length > 0 ? (
                  <div className="task-detail-group">
                    <h5>Verification Outcome</h5>
                    <p>
                      <strong>Status:</strong> {formatLabel(verifierOutcome.status || 'unknown')}
                    </p>
                    <p>
                      <strong>Summary:</strong> {verifierOutcome.summary || 'No summary reported.'}
                    </p>
                    {verifierOutcome.items.length > 0 ? (
                      <ul>
                        {verifierOutcome.items.map((item) => (
                          <li key={item.id}>
                            <strong>{formatLabel(item.label)}:</strong> {formatLabel(item.status)}
                            {item.message ? ` - ${item.message}` : ''}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                ) : (
                  <p className="task-detail-muted">
                    No verifier outcome captured for the latest run.
                  </p>
                )}
              </>
            ) : (
              <p className="task-detail-muted">No run observability metadata available yet.</p>
            )}
          </section>

          <section className="task-detail-section">
            <h4>Timeline</h4>
            {sortedTimeline.length > 0 ? (
              <ol className="task-timeline-list">
                {sortedTimeline.map((event, idx) => (
                  <li key={event.id}>
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
                      <li key={command.id}>
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
            <h4>Trace Observability</h4>
            {hasTraceObservability ? (
              <>
                {traceObservability.traceIds.length > 0 ? (
                  <div className="task-detail-group">
                    <h5>Trace IDs</h5>
                    <ul className="task-trace-list">
                      {traceObservability.traceIds.map((traceId) => (
                        <li key={traceId}>
                          <code>{traceId}</code>
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                {traceObservability.providerLinks.length > 0 ? (
                  <div className="task-detail-group">
                    <h5>Provider Deep Links</h5>
                    <ul className="task-trace-list">
                      {traceObservability.providerLinks.map((link) => (
                        <li key={link.url}>
                          <strong className="task-trace-provider">{link.provider}</strong>
                          <a
                            className="task-trace-url"
                            href={link.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            title={link.url}
                          >
                            {link.url}
                          </a>
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                {traceObservability.spanStatusCounts.length > 0 ? (
                  <div className="task-detail-group">
                    <h5>Span Status Summary</h5>
                    <ul className="task-trace-list">
                      {traceObservability.spanStatusCounts.map((entry) => (
                        <li key={entry.status}>
                          <strong>{formatLabel(entry.status)}:</strong> {entry.count}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </>
            ) : (
              <p className="task-detail-muted">No trace metadata available yet.</p>
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
