import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { BookOpen, Trash2 } from 'lucide-react';
import { api } from '../services/api';
import { DashboardLayout } from './layout/DashboardLayout';
import { PersonalMemorySnapshot, ProjectMemorySnapshot } from '../types/memory';

const KNOWLEDGE_BASE_REFETCH_INTERVAL_MS = 30000;
const KNOWLEDGE_BASE_PAGE_SIZE = 50;
const KNOWLEDGE_BASE_MAX_LIMIT = 200;
const PERSONAL_FILTER_DEBOUNCE_MS = 300;

function parseMemoryValue(raw: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(raw);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Memory value must be a JSON object.');
  }
  return parsed as Record<string, unknown>;
}

function parseConfidence(raw: string): number {
  const normalized = raw.trim();
  if (normalized.length === 0) {
    return 1.0;
  }
  const parsed = Number(normalized);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) {
    throw new Error('Confidence must be a number between 0 and 1.');
  }
  return parsed;
}

function normalizeOptional(value: string): string | undefined {
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : undefined;
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) {
    return 'Not verified';
  }
  return new Date(value).toLocaleString();
}

export function KnowledgeBasePage() {
  const [personalUserId, setPersonalUserId] = React.useState('');
  const [personalMemoryKey, setPersonalMemoryKey] = React.useState('');
  const [personalValueJson, setPersonalValueJson] = React.useState('{\n  \n}');
  const [personalSource, setPersonalSource] = React.useState('');
  const [personalScope, setPersonalScope] = React.useState('');
  const [personalConfidence, setPersonalConfidence] = React.useState('1.0');
  const [personalRequiresVerification, setPersonalRequiresVerification] = React.useState(true);
  const [personalError, setPersonalError] = React.useState<string | null>(null);
  const [personalSaving, setPersonalSaving] = React.useState(false);
  const [personalDeletingEntryId, setPersonalDeletingEntryId] = React.useState<string | null>(null);
  const [personalLimit, setPersonalLimit] = React.useState(KNOWLEDGE_BASE_PAGE_SIZE);
  const [personalUserFilter, setPersonalUserFilter] = React.useState('');

  const [projectRepoUrl, setProjectRepoUrl] = React.useState('');
  const [projectMemoryKey, setProjectMemoryKey] = React.useState('');
  const [projectValueJson, setProjectValueJson] = React.useState('{\n  \n}');
  const [projectSource, setProjectSource] = React.useState('');
  const [projectScope, setProjectScope] = React.useState('');
  const [projectConfidence, setProjectConfidence] = React.useState('1.0');
  const [projectRequiresVerification, setProjectRequiresVerification] = React.useState(true);
  const [projectError, setProjectError] = React.useState<string | null>(null);
  const [projectSaving, setProjectSaving] = React.useState(false);
  const [projectDeletingEntryId, setProjectDeletingEntryId] = React.useState<string | null>(null);
  const [projectLimit, setProjectLimit] = React.useState(KNOWLEDGE_BASE_PAGE_SIZE);

  React.useEffect(() => {
    const timer = window.setTimeout(() => {
      setPersonalUserFilter(personalUserId.trim());
    }, PERSONAL_FILTER_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [personalUserId]);

  const personalQueryEnabled = personalUserFilter.length > 0;

  React.useEffect(() => {
    setPersonalLimit(KNOWLEDGE_BASE_PAGE_SIZE);
  }, [personalUserFilter]);

  const {
    data: personalEntries = [],
    isLoading: personalLoading,
    isFetching: personalFetching,
    error: personalLoadError,
    refetch: refetchPersonal,
  } = useQuery({
    queryKey: ['knowledge-base', 'personal', personalUserFilter, personalLimit],
    queryFn: () => api.listPersonalMemory(personalUserFilter, personalLimit, 0),
    enabled: personalQueryEnabled,
    refetchInterval: KNOWLEDGE_BASE_REFETCH_INTERVAL_MS,
  });

  const {
    data: projectEntries = [],
    isLoading: projectLoading,
    isFetching: projectFetching,
    error: projectLoadError,
    refetch: refetchProject,
  } = useQuery({
    queryKey: ['knowledge-base', 'project', projectLimit],
    queryFn: () => api.listProjectMemory(undefined, projectLimit, 0),
    refetchInterval: KNOWLEDGE_BASE_REFETCH_INTERVAL_MS,
  });

  const isLoading = projectLoading || (personalQueryEnabled && personalLoading);
  const personalQueryError = personalQueryEnabled ? (personalLoadError as Error | null) : null;
  const projectQueryError = projectLoadError as Error | null;
  const personalHasMore =
    personalQueryEnabled &&
    personalEntries.length >= personalLimit &&
    personalLimit < KNOWLEDGE_BASE_MAX_LIMIT;
  const projectHasMore =
    projectEntries.length >= projectLimit && projectLimit < KNOWLEDGE_BASE_MAX_LIMIT;

  const handleSavePersonal = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPersonalError(null);
    setPersonalSaving(true);
    try {
      await api.upsertPersonalMemory({
        user_id: personalUserId.trim(),
        memory_key: personalMemoryKey.trim(),
        value: parseMemoryValue(personalValueJson),
        source: normalizeOptional(personalSource),
        scope: normalizeOptional(personalScope),
        confidence: parseConfidence(personalConfidence),
        requires_verification: personalRequiresVerification,
      });
      setPersonalMemoryKey('');
      setPersonalValueJson('{\n  \n}');
      await refetchPersonal();
    } catch (error) {
      setPersonalError((error as Error).message);
    } finally {
      setPersonalSaving(false);
    }
  };

  const handleSaveProject = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setProjectError(null);
    setProjectSaving(true);
    try {
      await api.upsertProjectMemory({
        repo_url: projectRepoUrl.trim(),
        memory_key: projectMemoryKey.trim(),
        value: parseMemoryValue(projectValueJson),
        source: normalizeOptional(projectSource),
        scope: normalizeOptional(projectScope),
        confidence: parseConfidence(projectConfidence),
        requires_verification: projectRequiresVerification,
      });
      setProjectMemoryKey('');
      setProjectValueJson('{\n  \n}');
      await refetchProject();
    } catch (error) {
      setProjectError((error as Error).message);
    } finally {
      setProjectSaving(false);
    }
  };

  const handleDeletePersonal = async (entry: PersonalMemorySnapshot) => {
    if (!window.confirm(`Delete personal memory "${entry.memory_key}" for user "${entry.user_id}"?`)) {
      return;
    }

    setPersonalDeletingEntryId(entry.memory_id);
    try {
      await api.deletePersonalMemory(entry.user_id, entry.memory_key);
      await refetchPersonal();
    } catch (error) {
      setPersonalError((error as Error).message);
    } finally {
      setPersonalDeletingEntryId(null);
    }
  };

  const handleDeleteProject = async (entry: ProjectMemorySnapshot) => {
    if (!window.confirm(`Delete project memory "${entry.memory_key}" for repo "${entry.repo_url}"?`)) {
      return;
    }

    setProjectDeletingEntryId(entry.memory_id);
    try {
      await api.deleteProjectMemory(entry.repo_url, entry.memory_key);
      await refetchProject();
    } catch (error) {
      setProjectError((error as Error).message);
    } finally {
      setProjectDeletingEntryId(null);
    }
  };

  return (
    <DashboardLayout>
      <div className="page-header">
        <h1>Knowledge Base</h1>
        <p className="page-subtitle">
          Manage skeptical memory entries with confidence and verification metadata.
        </p>
      </div>

      {isLoading ? (
        <div className="loading-container">
          <div className="spinner"></div>
          <p>Loading knowledge base...</p>
        </div>
      ) : (
        <div className="knowledge-grid">
          <section className="card knowledge-section">
            <h2>Personal Memory</h2>
            <p className="knowledge-section-subtitle">User-scoped memory entries</p>
            <form className="knowledge-form" onSubmit={handleSavePersonal}>
              <label htmlFor="personal-user-id">Personal User ID</label>
              <input
                id="personal-user-id"
                value={personalUserId}
                onChange={(event) => setPersonalUserId(event.target.value)}
                required
              />

              <label htmlFor="personal-memory-key">Personal Memory Key</label>
              <input
                id="personal-memory-key"
                value={personalMemoryKey}
                onChange={(event) => setPersonalMemoryKey(event.target.value)}
                required
              />

              <label htmlFor="personal-memory-value">Personal Memory Value (JSON object)</label>
              <textarea
                id="personal-memory-value"
                value={personalValueJson}
                onChange={(event) => setPersonalValueJson(event.target.value)}
                rows={5}
                required
              />

              <label htmlFor="personal-memory-source">Personal Source</label>
              <input
                id="personal-memory-source"
                value={personalSource}
                onChange={(event) => setPersonalSource(event.target.value)}
                placeholder="operator, sandbox_run, user_instruction..."
              />

              <label htmlFor="personal-memory-scope">Personal Scope</label>
              <input
                id="personal-memory-scope"
                value={personalScope}
                onChange={(event) => setPersonalScope(event.target.value)}
                placeholder="global, repo, branch..."
              />

              <label htmlFor="personal-memory-confidence">Personal Confidence (0.0-1.0)</label>
              <input
                id="personal-memory-confidence"
                type="number"
                step="0.01"
                min="0"
                max="1"
                value={personalConfidence}
                onChange={(event) => setPersonalConfidence(event.target.value)}
                required
              />

              <label className="knowledge-checkbox">
                <input
                  type="checkbox"
                  checked={personalRequiresVerification}
                  onChange={(event) => setPersonalRequiresVerification(event.target.checked)}
                />
                <span>Requires verification</span>
              </label>

              {personalError ? <p className="card-error-text">{personalError}</p> : null}
              <button className="btn-primary" type="submit" disabled={personalSaving}>
                {personalSaving ? 'Saving...' : 'Save Personal Entry'}
              </button>
            </form>

            <div className="knowledge-list">
              {personalQueryError ? (
                <p className="card-error-text">{personalQueryError.message}</p>
              ) : !personalQueryEnabled ? (
                <p className="session-context-muted">
                  Enter a personal user ID above to load entries.
                </p>
              ) : personalEntries.length === 0 ? (
                <p className="session-context-muted">No personal entries found.</p>
              ) : (
                personalEntries.map((entry) => (
                  <article key={entry.memory_id} className="knowledge-entry">
                    <header className="knowledge-entry-header">
                      <div>
                        <h3>{entry.memory_key}</h3>
                        <p>User: {entry.user_id}</p>
                      </div>
                      <button
                        className="btn-icon-sm"
                        type="button"
                        onClick={() => handleDeletePersonal(entry)}
                        title="Delete entry"
                        aria-label={`Delete personal memory ${entry.memory_key}`}
                        disabled={personalDeletingEntryId === entry.memory_id}
                      >
                        <Trash2 size={14} />
                      </button>
                    </header>
                    <div className="knowledge-entry-meta">
                      <span>Confidence: {entry.confidence.toFixed(2)}</span>
                      <span>Needs verification: {entry.requires_verification ? 'yes' : 'no'}</span>
                      <span>Verified at: {formatTimestamp(entry.last_verified_at)}</span>
                    </div>
                    <pre>{JSON.stringify(entry.value, null, 2)}</pre>
                  </article>
                ))
              )}
            </div>
            {personalHasMore ? (
              <button
                type="button"
                className="knowledge-load-more"
                onClick={() =>
                  setPersonalLimit((current) =>
                    Math.min(current + KNOWLEDGE_BASE_PAGE_SIZE, KNOWLEDGE_BASE_MAX_LIMIT)
                  )
                }
                disabled={personalFetching}
              >
                {personalFetching ? 'Loading...' : 'Load More Personal Entries'}
              </button>
            ) : null}
          </section>

          <section className="card knowledge-section">
            <h2>Project Memory</h2>
            <p className="knowledge-section-subtitle">Repository-scoped memory entries</p>
            <form className="knowledge-form" onSubmit={handleSaveProject}>
              <label htmlFor="project-repo-url">Project Repository URL</label>
              <input
                id="project-repo-url"
                value={projectRepoUrl}
                onChange={(event) => setProjectRepoUrl(event.target.value)}
                required
              />

              <label htmlFor="project-memory-key">Project Memory Key</label>
              <input
                id="project-memory-key"
                value={projectMemoryKey}
                onChange={(event) => setProjectMemoryKey(event.target.value)}
                required
              />

              <label htmlFor="project-memory-value">Project Memory Value (JSON object)</label>
              <textarea
                id="project-memory-value"
                value={projectValueJson}
                onChange={(event) => setProjectValueJson(event.target.value)}
                rows={5}
                required
              />

              <label htmlFor="project-memory-source">Project Source</label>
              <input
                id="project-memory-source"
                value={projectSource}
                onChange={(event) => setProjectSource(event.target.value)}
                placeholder="repo_analysis, test_run..."
              />

              <label htmlFor="project-memory-scope">Project Scope</label>
              <input
                id="project-memory-scope"
                value={projectScope}
                onChange={(event) => setProjectScope(event.target.value)}
                placeholder="repo, branch..."
              />

              <label htmlFor="project-memory-confidence">Project Confidence (0.0-1.0)</label>
              <input
                id="project-memory-confidence"
                type="number"
                step="0.01"
                min="0"
                max="1"
                value={projectConfidence}
                onChange={(event) => setProjectConfidence(event.target.value)}
                required
              />

              <label className="knowledge-checkbox">
                <input
                  type="checkbox"
                  checked={projectRequiresVerification}
                  onChange={(event) => setProjectRequiresVerification(event.target.checked)}
                />
                <span>Requires verification</span>
              </label>

              {projectError ? <p className="card-error-text">{projectError}</p> : null}
              <button className="btn-primary" type="submit" disabled={projectSaving}>
                {projectSaving ? 'Saving...' : 'Save Project Entry'}
              </button>
            </form>

            <div className="knowledge-list">
              {projectQueryError ? (
                <div className="error-container">
                  <h3>Error loading project memory</h3>
                  <p>{projectQueryError.message}</p>
                  <button onClick={() => refetchProject()} className="btn-primary" type="button">
                    Retry Project Memory
                  </button>
                </div>
              ) : projectEntries.length === 0 ? (
                <p className="session-context-muted">No project entries found.</p>
              ) : (
                projectEntries.map((entry) => (
                  <article key={entry.memory_id} className="knowledge-entry">
                    <header className="knowledge-entry-header">
                      <div>
                        <h3>{entry.memory_key}</h3>
                        <p>{entry.repo_url}</p>
                      </div>
                      <button
                        className="btn-icon-sm"
                        type="button"
                        onClick={() => handleDeleteProject(entry)}
                        title="Delete entry"
                        aria-label={`Delete project memory ${entry.memory_key}`}
                        disabled={projectDeletingEntryId === entry.memory_id}
                      >
                        <Trash2 size={14} />
                      </button>
                    </header>
                    <div className="knowledge-entry-meta">
                      <span>Confidence: {entry.confidence.toFixed(2)}</span>
                      <span>Needs verification: {entry.requires_verification ? 'yes' : 'no'}</span>
                      <span>Verified at: {formatTimestamp(entry.last_verified_at)}</span>
                    </div>
                    <pre>{JSON.stringify(entry.value, null, 2)}</pre>
                  </article>
                ))
              )}
            </div>
            {projectQueryError ? null : projectHasMore ? (
              <button
                type="button"
                className="knowledge-load-more"
                onClick={() =>
                  setProjectLimit((current) =>
                    Math.min(current + KNOWLEDGE_BASE_PAGE_SIZE, KNOWLEDGE_BASE_MAX_LIMIT)
                  )
                }
                disabled={projectFetching}
              >
                {projectFetching ? 'Loading...' : 'Load More Project Entries'}
              </button>
            ) : null}
          </section>
        </div>
      )}

      <div className="knowledge-page-footer">
        <BookOpen size={16} />
        <p>Memory entries are hints, not ground truth. Verify before relying on them.</p>
      </div>
    </DashboardLayout>
  );
}
