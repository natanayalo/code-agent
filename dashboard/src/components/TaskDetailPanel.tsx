import React from 'react';
import { X } from 'lucide-react';
import { TaskSnapshot } from '../types/task';
import { TaskApprovalSection } from './TaskApprovalSection';

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

function formatLabel(value: string | null | undefined): string {
  const normalized = (value || '').trim();
  if (!normalized) {
    return 'unknown';
  }
  return normalized
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase());
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

function normalizeToken(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, '');
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
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
  const hostLabels = host.split('.').filter(Boolean);
  if (hostMatchesDomain(host, 'smith.langchain.com')) return 'LangSmith';
  if (hostMatchesDomain(host, 'langfuse.com') || hostLabels.includes('langfuse')) return 'Langfuse';
  if (hostLabels.includes('arize') || hostLabels.includes('phoenix')) return 'Phoenix';
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
  const run = task?.latest_run ?? null;
  const runCommands = run?.commands_run ?? [];
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
                          <strong>{link.provider}</strong>{' '}
                          <a href={link.url} target="_blank" rel="noopener noreferrer">
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
