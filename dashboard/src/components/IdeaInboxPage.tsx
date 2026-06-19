import { useMemo, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { CheckCircle, Lightbulb, Sparkles, XCircle } from 'lucide-react';
import { api } from '../services/api';
import {
  FrictionReportMetadata,
  ImprovementScoringMetadata,
  ImprovementSuggestionMetadata,
  ProposalSnapshot,
  ProposalStatus,
  ProposalType,
} from '../types/proposal';
import { DashboardLayout } from './layout/DashboardLayout';

type ProposalFilter = 'all' | ProposalType.SCOUT | ProposalType.REFLECTION;

const PROPOSAL_FILTERS: Array<{ label: string; value: ProposalFilter }> = [
  { label: 'All', value: 'all' },
  { label: 'Ideas', value: ProposalType.SCOUT },
  { label: 'Improvements', value: ProposalType.REFLECTION },
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return isRecord(value) ? value : null;
}

function hasRecordValues(value: unknown): value is Record<string, unknown> {
  return isRecord(value) && Object.keys(value).length > 0;
}

function getProposalTypeLabel(filter: ProposalFilter): string {
  if (filter === ProposalType.SCOUT) {
    return 'ideas';
  }
  if (filter === ProposalType.REFLECTION) {
    return 'improvements';
  }
  return 'proposals';
}

function formatLabel(value: unknown, fallback = 'Not set'): string {
  if (typeof value === 'string' && value.trim().length > 0) {
    return value
      .replace(/_/g, ' ')
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return fallback;
}

function formatText(value: unknown, fallback = 'Not set'): string {
  if (typeof value === 'string' && value.trim().length > 0) {
    return value.trim();
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return fallback;
}

function formatDate(value: string | null | undefined): string {
  if (!value) {
    return 'N/A';
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? 'N/A'
    : date.toLocaleString('en-US', { timeZone: 'UTC', timeZoneName: 'short' });
}

function getProposalType(proposal: ProposalSnapshot): ProposalType {
  return proposal.proposal_type === ProposalType.REFLECTION
    ? ProposalType.REFLECTION
    : ProposalType.SCOUT;
}

function getImprovementSuggestion(
  proposal: ProposalSnapshot,
): ImprovementSuggestionMetadata | null {
  const candidate = asRecord(proposal.metadata_payload?.improvement_suggestion);
  return candidate ? (candidate as ImprovementSuggestionMetadata) : null;
}

function getFrictionReport(proposal: ProposalSnapshot): FrictionReportMetadata | null {
  const candidate = asRecord(proposal.metadata_payload?.friction_report);
  return candidate ? (candidate as FrictionReportMetadata) : null;
}

function getScoring(proposal: ProposalSnapshot): ImprovementScoringMetadata | null {
  const candidate = asRecord(proposal.metadata_payload?.scoring);
  return candidate ? (candidate as ImprovementScoringMetadata) : null;
}

function scoreTone(label: string, value: unknown): string {
  if (typeof value !== 'string') {
    return 'neutral';
  }
  const normalized = value.trim().toLowerCase();
  const isHighSignal = ['high', 'large', 'required'].includes(normalized);
  const isLowSignal = ['low', 'small', 'none'].includes(normalized);

  if (label === 'Value') {
    if (isHighSignal) {
      return 'success';
    }
    if (isLowSignal) {
      return 'warning';
    }
    return 'medium';
  }

  if (isHighSignal) {
    return 'warning';
  }
  if (isLowSignal) {
    return 'success';
  }
  return 'medium';
}

function renderMetadataValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.map(String).join('\n');
  }
  if (isRecord(value)) {
    return 'Unserializable Object value';
  }
  return String(value);
}

function ProposalTypeBadge({ proposal }: { proposal: ProposalSnapshot }) {
  const proposalType = getProposalType(proposal);
  if (proposalType === ProposalType.REFLECTION) {
    return (
      <span className="proposal-type-badge proposal-type-reflection">
        <Sparkles size={14} />
        Improvement
      </span>
    );
  }
  return (
    <span className="proposal-type-badge proposal-type-scout">
      <Lightbulb size={14} />
      Scout idea
    </span>
  );
}

function ReflectionProposalDetails({ proposal }: { proposal: ProposalSnapshot }) {
  const suggestion = getImprovementSuggestion(proposal);
  const frictionReport = getFrictionReport(proposal);
  const scoring = getScoring(proposal);

  if (!suggestion && !frictionReport && !scoring) {
    return null;
  }

  const scoreFields = suggestion
    ? [
        { label: 'Value', value: suggestion.value },
        { label: 'Effort', value: suggestion.effort },
        { label: 'Risk', value: suggestion.risk },
        { label: 'Layer', value: suggestion.layer_impact },
        { label: 'HITL', value: suggestion.hitl_need },
      ]
    : [];

  return (
    <div className="proposal-reflection-details">
      {suggestion ? (
        <>
          <dl className="proposal-score-grid" aria-label="Improvement scoring fields">
            {scoreFields.map((field) => (
              <div key={field.label} className="proposal-score-item">
                <dt>{field.label}</dt>
                <dd className={`proposal-score-value score-${scoreTone(field.label, field.value)}`}>
                  {formatLabel(field.value)}
                </dd>
              </div>
            ))}
          </dl>
          <div className="proposal-validation-path">
            <strong>Validation:</strong> {formatText(suggestion.validation_path)}
          </div>
        </>
      ) : null}

      {scoring?.rationale ? (
        <p className="proposal-scoring-rationale">{scoring.rationale}</p>
      ) : null}

      {frictionReport ? (
        <details className="proposal-details-panel">
          <summary>Friction evidence</summary>
          <dl className="proposal-evidence-grid">
            <div>
              <dt>Source</dt>
              <dd>{formatLabel(frictionReport.source)}</dd>
            </div>
            <div>
              <dt>Impact</dt>
              <dd>{formatLabel(frictionReport.impact)}</dd>
            </div>
            {frictionReport.description ? (
              <div className="proposal-evidence-wide">
                <dt>Description</dt>
                <dd>{frictionReport.description}</dd>
              </div>
            ) : null}
            {hasRecordValues(frictionReport.context) ? (
              <div className="proposal-evidence-wide">
                <dt>Context</dt>
                <dd>
                  <pre className="json-viewer">
                    {JSON.stringify(frictionReport.context, null, 2)}
                  </pre>
                </dd>
              </div>
            ) : null}
          </dl>
        </details>
      ) : null}
    </div>
  );
}

function ScoutProposalDetails({ proposal }: { proposal: ProposalSnapshot }) {
  const filesChanged = proposal.metadata_payload?.files_changed;
  const diffText = proposal.metadata_payload?.diff_text;
  if (!proposal.content && filesChanged == null && diffText == null) {
    return null;
  }

  return (
    <details className="proposal-details-panel">
      <summary>View Details</summary>
      {proposal.content ? <pre className="json-viewer">{proposal.content}</pre> : null}
      {filesChanged != null ? (
        <div className="metadata-section">
          <strong>Files Changed:</strong>
          <pre className="json-viewer">{renderMetadataValue(filesChanged)}</pre>
        </div>
      ) : null}
      {diffText != null ? (
        <div className="metadata-section">
          <strong>Diff:</strong>
          <pre className="json-viewer">{renderMetadataValue(diffText)}</pre>
        </div>
      ) : null}
    </details>
  );
}

export function IdeaInboxPage() {
  const queryClient = useQueryClient();
  const [actionError, setActionError] = useState<string | null>(null);
  const [activeFilter, setActiveFilter] = useState<ProposalFilter>('all');

  const { data: proposals = [], isLoading, error, refetch } = useQuery({
    queryKey: ['proposals', ProposalStatus.PENDING_REVIEW],
    queryFn: () => api.listProposals(ProposalStatus.PENDING_REVIEW),
    refetchInterval: 30000,
  });

  const counts = useMemo<Record<ProposalFilter, number>>(() => {
    return proposals.reduce(
      (acc, proposal) => {
        acc.all += 1;
        acc[getProposalType(proposal)] += 1;
        return acc;
      },
      { all: 0, [ProposalType.SCOUT]: 0, [ProposalType.REFLECTION]: 0 },
    );
  }, [proposals]);

  const filteredProposals = useMemo(() => {
    if (activeFilter === 'all') {
      return proposals;
    }
    return proposals.filter((proposal) => getProposalType(proposal) === activeFilter);
  }, [activeFilter, proposals]);

  const acceptMutation = useMutation({
    mutationFn: (proposalId: string) => api.acceptProposal(proposalId),
    onMutate: () => {
      setActionError(null);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['proposals'] });
      queryClient.invalidateQueries({ queryKey: ['tasks'] });
      setActionError(null);
    },
    onError: (err) => {
      const msg = err instanceof Error ? err.message : 'Failed to accept proposal';
      setActionError(msg);
    },
  });

  const rejectMutation = useMutation({
    mutationFn: (proposalId: string) => api.rejectProposal(proposalId),
    onMutate: () => {
      setActionError(null);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['proposals'] });
      setActionError(null);
    },
    onError: (err) => {
      const msg = err instanceof Error ? err.message : 'Failed to reject proposal';
      setActionError(msg);
    },
  });

  const handleAccept = (proposal: ProposalSnapshot) => {
    acceptMutation.mutate(proposal.proposal_id);
  };

  const handleReject = (proposal: ProposalSnapshot) => {
    const noun = getProposalType(proposal) === ProposalType.REFLECTION ? 'improvement' : 'idea';
    if (window.confirm(`Are you sure you want to reject this ${noun}?`)) {
      rejectMutation.mutate(proposal.proposal_id);
    }
  };

  return (
    <DashboardLayout>
      <div className="metrics-page proposals-page">
        <header className="metrics-header proposals-header">
          <div className="header-title">
            <Lightbulb className="header-icon" />
            <h2>Review Queue</h2>
          </div>
          <p className="header-description">
            Scout ideas and structural improvements waiting for operator review.
          </p>
        </header>

        <div className="proposal-filter-bar" role="group" aria-label="Proposal type filters">
          {PROPOSAL_FILTERS.map((filter) => (
            <button
              key={filter.value}
              className={`proposal-filter-button ${activeFilter === filter.value ? 'active' : ''}`}
              aria-pressed={activeFilter === filter.value}
              aria-label={`Show ${filter.label.toLowerCase()} proposals`}
              onClick={() => setActiveFilter(filter.value)}
            >
              <span>{filter.label}</span>
              <span className="proposal-filter-count">{counts[filter.value]}</span>
            </button>
          ))}
        </div>

        {error ? (
          <div className="error-banner">
            Failed to load proposals: {error instanceof Error ? error.message : String(error)}
            <button onClick={() => refetch()} className="retry-button">Retry</button>
          </div>
        ) : null}

        {actionError ? (
          <div className="error-banner">
            Action failed: {actionError}
            <button onClick={() => setActionError(null)} className="retry-button">Dismiss</button>
          </div>
        ) : null}

        <div className="proposals-list">
          {isLoading ? (
            <div className="loading-state">Loading proposals...</div>
          ) : filteredProposals.length === 0 ? (
            <div className="empty-state">
              <Lightbulb size={48} className="empty-icon" />
              <h3>No pending {getProposalTypeLabel(activeFilter)}</h3>
              <p>New review items appear here when generated by Scout or reflection runs.</p>
            </div>
          ) : (
            <div className="card-grid proposals-grid">
              {filteredProposals.map((proposal) => {
                const proposalType = getProposalType(proposal);
                const acceptLabel = proposalType === ProposalType.REFLECTION
                  ? 'Approve Improvement'
                  : 'Accept Idea';

                return (
                  <article
                    key={proposal.proposal_id}
                    className={`memory-card proposal-card proposal-card-${proposalType}`}
                  >
                    <div className="proposal-card-header">
                      <div className="proposal-title-group">
                        <div className="proposal-meta-row">
                          <ProposalTypeBadge proposal={proposal} />
                          <span className="proposal-created-at">
                            {formatDate(proposal.created_at)}
                          </span>
                        </div>
                        <h3 className="memory-key">{proposal.title}</h3>
                      </div>
                    </div>

                    <div className="memory-content proposal-card-content">
                      <p>{proposal.summary}</p>
                      {proposalType === ProposalType.REFLECTION ? (
                        <ReflectionProposalDetails proposal={proposal} />
                      ) : (
                        <ScoutProposalDetails proposal={proposal} />
                      )}
                    </div>

                    <div className="memory-footer proposal-actions">
                      <button
                        className="button button-success"
                        onClick={() => handleAccept(proposal)}
                        disabled={acceptMutation.isPending || rejectMutation.isPending}
                      >
                        <CheckCircle size={16} /> {acceptLabel}
                      </button>
                      <button
                        className="button button-danger"
                        onClick={() => handleReject(proposal)}
                        disabled={acceptMutation.isPending || rejectMutation.isPending}
                      >
                        <XCircle size={16} /> Reject
                      </button>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </DashboardLayout>
  );
}
