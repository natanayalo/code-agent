import React from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { BarChart3, BookOpen, Check, ClipboardList, Plus, Search, Trash2, X } from 'lucide-react';
import { api } from '../services/api';
import { DashboardLayout } from './layout/DashboardLayout';
import {
  KnowledgeBaseStatsSnapshot,
  MemoryInventoryCountSnapshot,
  MemoryProposalCategory,
  MemoryProposalSnapshot,
  PersonalMemorySnapshot,
  ProjectMemorySnapshot,
} from '../types/memory';

const KNOWLEDGE_BASE_REFETCH_INTERVAL_MS = 30000;
const KNOWLEDGE_BASE_PAGE_SIZE = 50;
const KNOWLEDGE_BASE_MAX_LIMIT = 200;
const SEARCH_DEBOUNCE_MS = 300;
const KNOWLEDGE_SEARCH_MIN_LENGTH = 2;
const KNOWLEDGE_SEARCH_LIMIT = 20;
const DEFAULT_MEMORY_VALUE_JSON = '{\n  \n}';
const HEADLINE_START = '__CA_MARK_START__';
const HEADLINE_END = '__CA_MARK_END__';
const EMPTY_STATS: KnowledgeBaseStatsSnapshot = {
  personal: { total: 0, requires_verification: 0 },
  project: null,
  project_global: { total: 0, requires_verification: 0 },
};

type KnowledgeBaseTab = 'browse' | 'review' | 'add';
type MemoryEntry = PersonalMemorySnapshot | ProjectMemorySnapshot;

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

function parseOptionalJsonObject(raw: string): Record<string, unknown> | undefined {
  if (raw.trim().length === 0) {
    return undefined;
  }
  return parseMemoryValue(raw);
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) {
    return 'Not verified';
  }
  return new Date(value).toLocaleString();
}

function formatMemoryCount(count: number): string {
  return `${count.toLocaleString()} ${count === 1 ? 'memory' : 'memories'}`;
}

function formatVerificationCount(count: number): string {
  return `${count.toLocaleString()} ${count === 1 ? 'needs' : 'need'} verification`;
}

function loadedSummary(
  label: string,
  loaded: number,
  stats: MemoryInventoryCountSnapshot | null,
): string {
  if (!stats) {
    return `Loaded ${loaded.toLocaleString()} ${label} ${loaded === 1 ? 'memory' : 'memories'}.`;
  }
  if (loaded >= KNOWLEDGE_BASE_MAX_LIMIT && stats.total > loaded) {
    return `Showing first ${loaded.toLocaleString()} of ${stats.total.toLocaleString()} ${label} memories.`;
  }
  return `Loaded ${loaded.toLocaleString()} of ${stats.total.toLocaleString()} ${label} ${
    stats.total === 1 ? 'memory' : 'memories'
  }.`;
}

function isProjectMemory(entry: MemoryEntry): entry is ProjectMemorySnapshot {
  return 'repo_url' in entry;
}

function renderHeadline(headline: string): React.ReactNode {
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  let partIndex = 0;

  while (cursor < headline.length) {
    const start = headline.indexOf(HEADLINE_START, cursor);
    if (start === -1) {
      parts.push(headline.slice(cursor));
      break;
    }

    if (start > cursor) {
      parts.push(headline.slice(cursor, start));
    }

    const end = headline.indexOf(HEADLINE_END, start + HEADLINE_START.length);
    if (end === -1) {
      parts.push(headline.slice(start));
      break;
    }

    const highlighted = headline.slice(start + HEADLINE_START.length, end);
    parts.push(<mark key={`headline-${partIndex}`}>{highlighted}</mark>);
    partIndex += 1;
    cursor = end + HEADLINE_END.length;
  }

  return parts;
}

export function KnowledgeBasePage() {
  const [activeTab, setActiveTab] = React.useState<KnowledgeBaseTab>('browse');
  const [personalMemoryKey, setPersonalMemoryKey] = React.useState('');
  const [personalValueJson, setPersonalValueJson] = React.useState(DEFAULT_MEMORY_VALUE_JSON);
  const [personalSource, setPersonalSource] = React.useState('');
  const [personalScope, setPersonalScope] = React.useState('');
  const [personalConfidence, setPersonalConfidence] = React.useState('1.0');
  const [personalRequiresVerification, setPersonalRequiresVerification] = React.useState(true);
  const [personalError, setPersonalError] = React.useState<string | null>(null);
  const [personalSaving, setPersonalSaving] = React.useState(false);
  const [personalDeletingEntryId, setPersonalDeletingEntryId] = React.useState<string | null>(null);
  const [searchInput, setSearchInput] = React.useState('');
  const [searchQuery, setSearchQuery] = React.useState('');

  const [proposalCategory, setProposalCategory] =
    React.useState<MemoryProposalCategory>('project');
  const [proposalMemoryKey, setProposalMemoryKey] = React.useState('');
  const [proposalValueJson, setProposalValueJson] = React.useState(DEFAULT_MEMORY_VALUE_JSON);
  const [proposalTitle, setProposalTitle] = React.useState('');
  const [proposalSummary, setProposalSummary] = React.useState('');
  const [proposalEvidenceJson, setProposalEvidenceJson] = React.useState('');
  const [proposalSource, setProposalSource] = React.useState('curated_corpus');
  const [proposalScope, setProposalScope] = React.useState('repo');
  const [proposalConfidence, setProposalConfidence] = React.useState('0.9');
  const [proposalRequiresVerification, setProposalRequiresVerification] = React.useState(false);
  const [proposalError, setProposalError] = React.useState<string | null>(null);
  const [proposalSaving, setProposalSaving] = React.useState(false);
  const [reviewingProposalId, setReviewingProposalId] = React.useState<string | null>(null);

  const [projectRepoUrl, setProjectRepoUrl] = React.useState('');
  const [projectMemoryKey, setProjectMemoryKey] = React.useState('');
  const [projectValueJson, setProjectValueJson] = React.useState(DEFAULT_MEMORY_VALUE_JSON);
  const [projectSource, setProjectSource] = React.useState('');
  const [projectScope, setProjectScope] = React.useState('');
  const [projectConfidence, setProjectConfidence] = React.useState('1.0');
  const [projectRequiresVerification, setProjectRequiresVerification] = React.useState(true);
  const [projectError, setProjectError] = React.useState<string | null>(null);
  const [projectSaving, setProjectSaving] = React.useState(false);
  const [projectDeletingEntryId, setProjectDeletingEntryId] = React.useState<string | null>(null);

  React.useEffect(() => {
    const timer = window.setTimeout(() => {
      setSearchQuery(searchInput.trim());
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [searchInput]);

  const projectScopeFilter = projectRepoUrl.trim();
  const searchMode = searchQuery.length >= KNOWLEDGE_SEARCH_MIN_LENGTH;
  const searchNeedsMoreCharacters = searchQuery.length > 0 && !searchMode;
  const personalSearchEnabled = searchMode;
  const projectSearchEnabled = searchMode && projectScopeFilter.length > 0;

  const resetPersonalForm = React.useCallback(() => {
    setPersonalMemoryKey('');
    setPersonalValueJson(DEFAULT_MEMORY_VALUE_JSON);
    setPersonalSource('');
    setPersonalScope('');
    setPersonalConfidence('1.0');
    setPersonalRequiresVerification(true);
  }, []);

  const resetProjectForm = React.useCallback(() => {
    setProjectMemoryKey('');
    setProjectValueJson(DEFAULT_MEMORY_VALUE_JSON);
    setProjectSource('');
    setProjectScope('');
    setProjectConfidence('1.0');
    setProjectRequiresVerification(true);
  }, []);

  const resetProposalForm = React.useCallback(() => {
    setProposalMemoryKey('');
    setProposalValueJson(DEFAULT_MEMORY_VALUE_JSON);
    setProposalTitle('');
    setProposalSummary('');
    setProposalEvidenceJson('');
  }, []);

  const {
    data: stats = EMPTY_STATS,
    isLoading: statsLoading,
    error: statsError,
    refetch: refetchStats,
  } = useQuery({
    queryKey: ['knowledge-base', 'stats', projectScopeFilter],
    queryFn: () => api.getKnowledgeBaseStats(projectScopeFilter || undefined),
    retry: false,
    refetchInterval: KNOWLEDGE_BASE_REFETCH_INTERVAL_MS,
  });

  const {
    data: pendingMemoryProposals = [],
    isLoading: pendingProposalsLoading,
    error: pendingProposalsError,
    refetch: refetchPendingProposals,
  } = useQuery({
    queryKey: ['knowledge-base', 'memory-proposals', 'pending'],
    queryFn: () => api.listMemoryProposals('pending_review'),
    retry: false,
    refetchInterval: KNOWLEDGE_BASE_REFETCH_INTERVAL_MS,
  });

  const {
    data: reviewedMemoryProposals = [],
    error: reviewedProposalsError,
    refetch: refetchReviewedProposals,
  } = useQuery({
    queryKey: ['knowledge-base', 'memory-proposals', 'reviewed'],
    queryFn: async () => {
      const [accepted, rejected] = await Promise.all([
        api.listMemoryProposals('accepted'),
        api.listMemoryProposals('rejected'),
      ]);
      return [...accepted, ...rejected].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    },
    retry: false,
    refetchInterval: KNOWLEDGE_BASE_REFETCH_INTERVAL_MS,
  });

  const {
    data: personalData,
    isLoading: personalLoading,
    isFetchingNextPage: personalFetchingNextPage,
    hasNextPage: personalHasNextPage,
    error: personalLoadError,
    refetch: refetchPersonal,
    fetchNextPage: fetchNextPersonalPage,
  } = useInfiniteQuery({
    queryKey: ['knowledge-base', 'personal'],
    queryFn: ({ pageParam = 0 }) =>
      api.listPersonalMemory(KNOWLEDGE_BASE_PAGE_SIZE, pageParam),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const loadedCount = allPages.reduce((count, page) => count + page.length, 0);
      if (lastPage.length < KNOWLEDGE_BASE_PAGE_SIZE || loadedCount >= KNOWLEDGE_BASE_MAX_LIMIT) {
        return undefined;
      }
      return loadedCount;
    },
    refetchInterval: KNOWLEDGE_BASE_REFETCH_INTERVAL_MS,
  });
  const personalEntries = React.useMemo(
    () => personalData?.pages.flatMap((page) => page) ?? [],
    [personalData]
  );
  const {
    data: personalSearchEntries = [],
    isLoading: personalSearchLoading,
    error: personalSearchError,
    refetch: refetchPersonalSearch,
  } = useQuery({
    queryKey: ['knowledge-base', 'personal-search', searchQuery],
    queryFn: () => api.searchPersonalMemory(searchQuery, KNOWLEDGE_SEARCH_LIMIT),
    enabled: personalSearchEnabled,
    retry: false,
  });

  const {
    data: projectData,
    isLoading: projectLoading,
    isFetchingNextPage: projectFetchingNextPage,
    hasNextPage: projectHasNextPage,
    error: projectLoadError,
    refetch: refetchProject,
    fetchNextPage: fetchNextProjectPage,
  } = useInfiniteQuery({
    queryKey: ['knowledge-base', 'project', projectScopeFilter],
    queryFn: ({ pageParam = 0 }) =>
      api.listProjectMemory(projectScopeFilter || undefined, KNOWLEDGE_BASE_PAGE_SIZE, pageParam),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const loadedCount = allPages.reduce((count, page) => count + page.length, 0);
      if (lastPage.length < KNOWLEDGE_BASE_PAGE_SIZE || loadedCount >= KNOWLEDGE_BASE_MAX_LIMIT) {
        return undefined;
      }
      return loadedCount;
    },
    refetchInterval: KNOWLEDGE_BASE_REFETCH_INTERVAL_MS,
  });
  const projectEntries = React.useMemo(
    () => projectData?.pages.flatMap((page) => page) ?? [],
    [projectData]
  );
  const {
    data: projectSearchEntries = [],
    isLoading: projectSearchLoading,
    error: projectSearchError,
    refetch: refetchProjectSearch,
  } = useQuery({
    queryKey: ['knowledge-base', 'project-search', projectScopeFilter, searchQuery],
    queryFn: () => api.searchProjectMemory(projectScopeFilter, searchQuery, KNOWLEDGE_SEARCH_LIMIT),
    enabled: projectSearchEnabled,
    retry: false,
  });

  const isLoading =
    activeTab === 'browse' &&
    (searchMode
      ? (personalSearchEnabled && personalSearchLoading) || (projectSearchEnabled && projectSearchLoading)
      : projectLoading || personalLoading);
  const personalQueryError = personalLoadError as Error | null;
  const projectQueryError = projectLoadError as Error | null;
  const personalSearchQueryError = personalSearchEnabled ? (personalSearchError as Error | null) : null;
  const projectSearchQueryError = projectSearchEnabled ? (projectSearchError as Error | null) : null;
  const personalHasMore = Boolean(personalHasNextPage);
  const projectHasMore = Boolean(projectHasNextPage);
  const projectBrowseStats = projectScopeFilter ? stats.project : stats.project_global;

  const refreshInventory = React.useCallback(async () => {
    await refetchStats();
    await refetchPersonal();
    await refetchProject();
    if (personalSearchEnabled) {
      await refetchPersonalSearch();
    }
    if (projectSearchEnabled) {
      await refetchProjectSearch();
    }
  }, [
    personalSearchEnabled,
    projectSearchEnabled,
    refetchPersonal,
    refetchPersonalSearch,
    refetchProject,
    refetchProjectSearch,
    refetchStats,
  ]);

  const refreshMemoryProposals = React.useCallback(async () => {
    await refetchPendingProposals();
    await refetchReviewedProposals();
  }, [refetchPendingProposals, refetchReviewedProposals]);

  function renderMemoryProposal(proposal: MemoryProposalSnapshot): React.ReactNode {
    const isPending = proposal.status === 'pending_review';
    return (
      <article key={proposal.proposal_id} className="knowledge-entry">
        <header className="knowledge-entry-header">
          <div>
            <h3>{proposal.title || proposal.memory_key}</h3>
            <p>
              {proposal.category === 'project'
                ? proposal.repo_url || 'Project memory'
                : 'Personal memory'}
            </p>
          </div>
          <span className={`proposal-status-badge status-${proposal.status}`}>
            {proposal.status.replace('_', ' ')}
          </span>
        </header>
        {proposal.summary ? (
          <p className="knowledge-section-subtitle">{proposal.summary}</p>
        ) : null}
        <div className="knowledge-entry-meta">
          <span>Key: {proposal.memory_key}</span>
          <span>Confidence: {proposal.confidence.toFixed(2)}</span>
          <span>Needs verification: {proposal.requires_verification ? 'yes' : 'no'}</span>
        </div>
        <pre>{JSON.stringify(proposal.value, null, 2)}</pre>
        {isPending ? (
          <div className="knowledge-review-actions">
            <button
              type="button"
              className="btn-primary"
              onClick={() => handleAcceptMemoryProposal(proposal.proposal_id)}
              disabled={reviewingProposalId === proposal.proposal_id}
            >
              <Check size={15} />
              Accept
            </button>
            <button
              type="button"
              className="knowledge-load-more"
              onClick={() => handleRejectMemoryProposal(proposal.proposal_id)}
              disabled={reviewingProposalId === proposal.proposal_id}
            >
              <X size={15} />
              Reject
            </button>
          </div>
        ) : null}
      </article>
    );
  }

  function renderMemoryEntry(entry: MemoryEntry): React.ReactNode {
    const projectEntry = isProjectMemory(entry);
    return (
      <article key={entry.memory_id} className="knowledge-entry">
        <header className="knowledge-entry-header">
          <div>
            <h3>{entry.memory_key}</h3>
            <p>{projectEntry ? entry.repo_url : 'Personal memory'}</p>
          </div>
          <button
            className="btn-icon-sm"
            type="button"
            onClick={() => (projectEntry ? handleDeleteProject(entry) : handleDeletePersonal(entry))}
            title="Delete entry"
            aria-label={`Delete ${projectEntry ? 'project' : 'personal'} memory ${entry.memory_key}`}
            disabled={
              projectEntry
                ? projectDeletingEntryId === entry.memory_id
                : personalDeletingEntryId === entry.memory_id
            }
          >
            <Trash2 size={14} />
          </button>
        </header>
        {entry.headline ? (
          <p className="knowledge-entry-headline">{renderHeadline(entry.headline)}</p>
        ) : null}
        <div className="knowledge-entry-meta">
          <span>Confidence: {entry.confidence.toFixed(2)}</span>
          <span>Needs verification: {entry.requires_verification ? 'yes' : 'no'}</span>
          <span>Verified at: {formatTimestamp(entry.last_verified_at)}</span>
        </div>
        <pre>{JSON.stringify(entry.value, null, 2)}</pre>
      </article>
    );
  }

  function renderPersonalListContent(): React.ReactNode {
    if (searchMode) {
      if (personalSearchQueryError) {
        return <p className="card-error-text">{personalSearchQueryError.message}</p>;
      }
      if (personalSearchEntries.length === 0) {
        return <p className="session-context-muted">No personal search results found.</p>;
      }
      return personalSearchEntries.map(renderMemoryEntry);
    }

    if (personalQueryError) {
      return <p className="card-error-text">{personalQueryError.message}</p>;
    }
    if (personalEntries.length === 0) {
      return <p className="session-context-muted">No personal entries found.</p>;
    }
    return personalEntries.map(renderMemoryEntry);
  }

  function renderProjectListContent(): React.ReactNode {
    if (searchMode) {
      if (projectSearchQueryError) {
        return (
          <div className="error-container">
            <h3>Error loading project search</h3>
            <p>{projectSearchQueryError.message}</p>
            <button
              onClick={() => refetchProjectSearch()}
              className="btn-primary"
              type="button"
            >
              Retry Project Search
            </button>
          </div>
        );
      }
      if (!projectSearchEnabled) {
        return (
          <p className="session-context-muted">
            Enter a project repository URL above to search project memory.
          </p>
        );
      }
      if (projectSearchEntries.length === 0) {
        return <p className="session-context-muted">No project search results found.</p>;
      }
      return projectSearchEntries.map(renderMemoryEntry);
    }

    if (projectQueryError) {
      return (
        <div className="error-container">
          <h3>Error loading project memory</h3>
          <p>{projectQueryError.message}</p>
          <button onClick={() => refetchProject()} className="btn-primary" type="button">
            Retry Project Memory
          </button>
        </div>
      );
    }
    if (projectEntries.length === 0) {
      return <p className="session-context-muted">No project entries found.</p>;
    }
    return projectEntries.map(renderMemoryEntry);
  }

  const handleSavePersonal = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPersonalError(null);
    setPersonalSaving(true);
    try {
      await api.upsertPersonalMemory({
        memory_key: personalMemoryKey.trim(),
        value: parseMemoryValue(personalValueJson),
        source: normalizeOptional(personalSource),
        scope: normalizeOptional(personalScope),
        confidence: parseConfidence(personalConfidence),
        requires_verification: personalRequiresVerification,
      });
      resetPersonalForm();
      await refreshInventory();
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
      resetProjectForm();
      await refreshInventory();
    } catch (error) {
      setProjectError((error as Error).message);
    } finally {
      setProjectSaving(false);
    }
  };

  const handleCreateMemoryProposal = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setProposalError(null);
    setProposalSaving(true);
    try {
      await api.createMemoryProposal({
        category: proposalCategory,
        repo_url: proposalCategory === 'project' ? projectRepoUrl.trim() : undefined,
        memory_key: proposalMemoryKey.trim(),
        value: parseMemoryValue(proposalValueJson),
        title: normalizeOptional(proposalTitle),
        summary: normalizeOptional(proposalSummary),
        evidence: parseOptionalJsonObject(proposalEvidenceJson),
        source: normalizeOptional(proposalSource),
        scope: normalizeOptional(proposalScope),
        confidence: parseConfidence(proposalConfidence),
        requires_verification: proposalRequiresVerification,
      });
      resetProposalForm();
      await refreshMemoryProposals();
    } catch (error) {
      setProposalError((error as Error).message);
    } finally {
      setProposalSaving(false);
    }
  };

  const handleAcceptMemoryProposal = async (proposalId: string) => {
    setProposalError(null);
    setReviewingProposalId(proposalId);
    try {
      await api.acceptMemoryProposal(proposalId);
      await refreshMemoryProposals();
      await refreshInventory();
    } catch (error) {
      setProposalError((error as Error).message);
    } finally {
      setReviewingProposalId(null);
    }
  };

  const handleRejectMemoryProposal = async (proposalId: string) => {
    setProposalError(null);
    setReviewingProposalId(proposalId);
    try {
      await api.rejectMemoryProposal(proposalId);
      await refreshMemoryProposals();
    } catch (error) {
      setProposalError((error as Error).message);
    } finally {
      setReviewingProposalId(null);
    }
  };

  const handleDeletePersonal = async (entry: PersonalMemorySnapshot) => {
    if (!window.confirm(`Delete personal memory "${entry.memory_key}"?`)) {
      return;
    }

    setPersonalDeletingEntryId(entry.memory_id);
    try {
      await api.deletePersonalMemory(entry.memory_key);
      await refreshInventory();
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
      await refreshInventory();
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
          Browse, search, and maintain skeptical memory with confidence and verification metadata.
        </p>
      </div>

      <div className="knowledge-tab-list" role="tablist" aria-label="Knowledge base mode">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === 'browse'}
          className={`knowledge-tab-button ${activeTab === 'browse' ? 'active' : ''}`}
          onClick={() => setActiveTab('browse')}
        >
          <BookOpen size={16} />
          Browse
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === 'review'}
          className={`knowledge-tab-button ${activeTab === 'review' ? 'active' : ''}`}
          onClick={() => setActiveTab('review')}
        >
          <ClipboardList size={16} />
          Review
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === 'add'}
          className={`knowledge-tab-button ${activeTab === 'add' ? 'active' : ''}`}
          onClick={() => setActiveTab('add')}
        >
          <Plus size={16} />
          Add Memory
        </button>
      </div>

      <section className="card knowledge-scope-panel" aria-label="Memory scope">
        <div>
          <h2>Memory Scope</h2>
          <p className="knowledge-section-subtitle">
            Project scope drives repository metrics, browsing, search, and new project entries.
          </p>
        </div>
        <div className="knowledge-scope-grid">
          <label htmlFor="project-repo-url">Project Repository URL</label>
          <input
            id="project-repo-url"
            value={projectRepoUrl}
            onChange={(event) => setProjectRepoUrl(event.target.value)}
            placeholder="https://github.com/owner/repo"
          />
        </div>
      </section>

      {activeTab === 'browse' ? (
        <>
          <section className="knowledge-stats-grid" aria-label="Memory inventory metrics">
            <article className="knowledge-stat-card">
              <BarChart3 size={16} />
              <span>Personal Memory</span>
              <strong>
                {statsLoading
                  ? 'Loading...'
                  : formatMemoryCount(stats.personal.total)}
              </strong>
              <small>{formatVerificationCount(stats.personal.requires_verification)}</small>
            </article>
            <article className="knowledge-stat-card">
              <BarChart3 size={16} />
              <span>Project scope</span>
              <strong>
                {statsLoading
                  ? 'Loading...'
                  : stats.project
                    ? formatMemoryCount(stats.project.total)
                    : 'No repo scope'}
              </strong>
              <small>
                {stats.project
                  ? formatVerificationCount(stats.project.requires_verification)
                  : 'Enter a repo URL'}
              </small>
            </article>
            <article className="knowledge-stat-card">
              <BarChart3 size={16} />
              <span>All project memory</span>
              <strong>
                {statsLoading ? 'Loading...' : formatMemoryCount(stats.project_global.total)}
              </strong>
              <small>{formatVerificationCount(stats.project_global.requires_verification)}</small>
            </article>
          </section>
          {statsError ? (
            <p className="card-error-text">Unable to load inventory metrics.</p>
          ) : null}

          <section className="card knowledge-search-panel">
            <div className="knowledge-search-header">
              <div>
                <h2>Search Memory</h2>
                <p className="knowledge-section-subtitle">
                  Search checks global personal memory and the current project repository scope.
                </p>
              </div>
              {searchInput.length > 0 ? (
                <button
                  className="knowledge-load-more"
                  type="button"
                  onClick={() => {
                    setSearchInput('');
                    setSearchQuery('');
                  }}
                >
                  Clear Search
                </button>
              ) : null}
            </div>
            <div className="knowledge-search-controls">
              <label htmlFor="knowledge-search-query">Search Query</label>
              <div className="knowledge-search-input-wrap">
                <Search size={15} />
                <input
                  id="knowledge-search-query"
                  value={searchInput}
                  onChange={(event) => setSearchInput(event.target.value)}
                  placeholder="Search memory keys and values..."
                />
              </div>
            </div>
            <div className="knowledge-search-summary">
              {searchMode ? (
                <p>
                  Searching for <strong>{searchQuery}</strong> with{' '}
                  {personalSearchEnabled ? `${personalSearchEntries.length} personal` : '0 personal'} and{' '}
                  {projectSearchEnabled ? `${projectSearchEntries.length} project` : '0 project'} matches.
                </p>
              ) : searchNeedsMoreCharacters ? (
                <p>Type at least 2 characters to start searching.</p>
              ) : (
                <p>Clear the query at any time to return to paginated browse mode.</p>
              )}
            </div>
          </section>

          {isLoading ? (
            <div className="loading-container">
              <div className="spinner"></div>
              <p>Loading knowledge base...</p>
            </div>
          ) : (
            <div className="knowledge-grid">
              <section className="card knowledge-section">
                <h2>Personal Memory</h2>
                <p className="knowledge-section-subtitle">
                  {searchMode
                    ? 'User-scoped search results'
                    : loadedSummary('personal', personalEntries.length, stats.personal)}
                </p>
                {personalError ? <p className="card-error-text">{personalError}</p> : null}
                <div className="knowledge-list">{renderPersonalListContent()}</div>
                {!searchMode && personalHasMore ? (
                  <button
                    type="button"
                    className="knowledge-load-more"
                    onClick={() => fetchNextPersonalPage()}
                    disabled={personalFetchingNextPage}
                  >
                    {personalFetchingNextPage ? 'Loading...' : 'Load More Personal Entries'}
                  </button>
                ) : null}
              </section>

              <section className="card knowledge-section">
                <h2>Project Memory</h2>
                <p className="knowledge-section-subtitle">
                  {searchMode
                    ? 'Repository-scoped search results'
                    : loadedSummary('project', projectEntries.length, projectBrowseStats)}
                </p>
                {projectError ? <p className="card-error-text">{projectError}</p> : null}
                <div className="knowledge-list">{renderProjectListContent()}</div>
                {searchMode || projectQueryError ? null : projectHasMore ? (
                  <button
                    type="button"
                    className="knowledge-load-more"
                    onClick={() => fetchNextProjectPage()}
                    disabled={projectFetchingNextPage}
                  >
                    {projectFetchingNextPage ? 'Loading...' : 'Load More Project Entries'}
                  </button>
                ) : null}
              </section>
            </div>
          )}
        </>
      ) : activeTab === 'review' ? (
        <div className="knowledge-grid">
          <section className="card knowledge-section">
            <h2>Pending Review</h2>
            <p className="knowledge-section-subtitle">
              Accept proposals to upsert durable memory, or reject them without writing memory.
            </p>
            {proposalError ? <p className="card-error-text">{proposalError}</p> : null}
            {pendingProposalsError ? (
              <p className="card-error-text">{(pendingProposalsError as Error).message}</p>
            ) : null}
            <div className="knowledge-list">
              {pendingProposalsLoading ? (
                <p className="session-context-muted">Loading memory proposals...</p>
              ) : pendingMemoryProposals.length === 0 ? (
                <p className="session-context-muted">No memory proposals pending review.</p>
              ) : (
                pendingMemoryProposals.map(renderMemoryProposal)
              )}
            </div>
          </section>

          <section className="card knowledge-section">
            <h2>Manual Proposal</h2>
            <p className="knowledge-section-subtitle">
              Seed curated memories as proposals first, then accept them after review.
            </p>
            <form className="knowledge-form" onSubmit={handleCreateMemoryProposal}>
              <label htmlFor="memory-proposal-category">Proposal Category</label>
              <select
                id="memory-proposal-category"
                value={proposalCategory}
                onChange={(event) => {
                  const category = event.target.value as MemoryProposalCategory;
                  setProposalCategory(category);
                  setProposalScope(category === 'project' ? 'repo' : 'global');
                }}
              >
                <option value="project">Project</option>
                <option value="personal">Personal</option>
              </select>

              <label htmlFor="memory-proposal-key">Memory Key</label>
              <input
                id="memory-proposal-key"
                value={proposalMemoryKey}
                onChange={(event) => setProposalMemoryKey(event.target.value)}
                required
              />

              <label htmlFor="memory-proposal-title">Title</label>
              <input
                id="memory-proposal-title"
                value={proposalTitle}
                onChange={(event) => setProposalTitle(event.target.value)}
              />

              <label htmlFor="memory-proposal-summary">Summary</label>
              <textarea
                id="memory-proposal-summary"
                value={proposalSummary}
                onChange={(event) => setProposalSummary(event.target.value)}
                rows={3}
              />

              <label htmlFor="memory-proposal-value">Memory Value (JSON object)</label>
              <textarea
                id="memory-proposal-value"
                value={proposalValueJson}
                onChange={(event) => setProposalValueJson(event.target.value)}
                rows={5}
                required
              />

              <label htmlFor="memory-proposal-evidence">Evidence (optional JSON object)</label>
              <textarea
                id="memory-proposal-evidence"
                value={proposalEvidenceJson}
                onChange={(event) => setProposalEvidenceJson(event.target.value)}
                rows={3}
              />

              <label htmlFor="memory-proposal-source">Source</label>
              <input
                id="memory-proposal-source"
                value={proposalSource}
                onChange={(event) => setProposalSource(event.target.value)}
              />

              <label htmlFor="memory-proposal-scope">Scope</label>
              <input
                id="memory-proposal-scope"
                value={proposalScope}
                onChange={(event) => setProposalScope(event.target.value)}
              />

              <label htmlFor="memory-proposal-confidence">Confidence (0.0-1.0)</label>
              <input
                id="memory-proposal-confidence"
                type="number"
                step="0.01"
                min="0"
                max="1"
                value={proposalConfidence}
                onChange={(event) => setProposalConfidence(event.target.value)}
                required
              />

              <label className="knowledge-checkbox">
                <input
                  type="checkbox"
                  checked={proposalRequiresVerification}
                  onChange={(event) => setProposalRequiresVerification(event.target.checked)}
                />
                <span>Requires verification</span>
              </label>

              <button
                className="btn-primary"
                type="submit"
                disabled={
                  proposalSaving ||
                  (proposalCategory === 'project' && projectRepoUrl.trim().length === 0)
                }
              >
                {proposalSaving ? 'Creating...' : 'Create Memory Proposal'}
              </button>
            </form>
          </section>

          <section className="card knowledge-section knowledge-section-wide">
            <h2>Reviewed</h2>
            {reviewedProposalsError ? (
              <p className="card-error-text">{(reviewedProposalsError as Error).message}</p>
            ) : null}
            <div className="knowledge-list">
              {reviewedMemoryProposals.length === 0 ? (
                <p className="session-context-muted">No reviewed memory proposals yet.</p>
              ) : (
                reviewedMemoryProposals.map(renderMemoryProposal)
              )}
            </div>
          </section>
        </div>
      ) : (
        <div className="knowledge-grid">
          <section className="card knowledge-section">
            <h2>Personal Memory</h2>
            <p className="knowledge-section-subtitle">Create or update operator-global memory entries</p>
            <form className="knowledge-form" onSubmit={handleSavePersonal}>
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
              <button
                className="btn-primary"
                type="submit"
                disabled={personalSaving}
              >
                {personalSaving ? 'Saving...' : 'Save Personal Entry'}
              </button>
            </form>
          </section>

          <section className="card knowledge-section">
            <h2>Project Memory</h2>
            <p className="knowledge-section-subtitle">Create or update repository-scoped memory entries</p>
            <form className="knowledge-form" onSubmit={handleSaveProject}>
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
              <button
                className="btn-primary"
                type="submit"
                disabled={projectSaving || projectRepoUrl.trim().length === 0}
              >
                {projectSaving ? 'Saving...' : 'Save Project Entry'}
              </button>
            </form>
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
