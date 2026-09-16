import React from 'react';
import {
  FileCode,
  Shield,
  Layers,
  AlertTriangle,
  Brain,
  Hash,
  Lock,
} from 'lucide-react';

export interface TruncationRecordData {
  field?: string;
  original_length?: number;
  truncated_length?: number;
  reason?: string;
  unit?: 'characters' | 'items';
  // Backward compatibility / aliases
  section?: string;
  original_count?: number;
  retained_count?: number;
}

export interface RepoFactsData {
  repo_url?: string | null;
  branch?: string | null;
  workspace_mode?: string | null;
  workspace_id?: string | null;
  commit_sha?: string | null;
  worktree_state_digest?: string | null;
  git_evidence_status?: string;
  git_evidence_reason?: string | null;
  has_agents_md?: boolean | null;
  detected_build_systems?: string[];
  workspace_identity_omission_reason?: string;
  // Backward compatibility / aliases
  workspace_type?: string;
  git_head_sha?: string | null;
}

export interface RepoSkillData {
  name: string;
  description?: string | null;
  relative_path?: string;
  path?: string;
}

export interface GatedMemoryEntryData {
  memory_key: string;
  value: unknown;
  category?: 'personal' | 'project';
  source?: string | null;
  confidence?: number;
  scope?: string | null;
  last_verified_at?: string | null;
  requires_verification?: boolean;
  staleness?: number;
  conflict?: string | null;
  risk?: string;
  advisory_strength?: number;
  gate_status?: string;
  gate_reason_codes?: string[];
}

export interface SessionContextData {
  active_goal?: string | null;
  decisions_made?: unknown;
  identified_risks?: unknown;
  files_touched?: string[];
}

export interface CapabilitySummaryData {
  read_only?: boolean;
  risk_level?: string;
  allowed_actions?: string[];
  forbidden_actions?: string[];
  delivery_mode?: string;
  network_enabled?: boolean;
  granted_secret_refs?: string[];
  worker_type?: string | null;
  worker_profile?: string | null;
  runtime_mode?: string | null;
  // Backward compatibility / aliases
  granted_tools?: string[];
  approval_capabilities?: string[];
  maintenance_actions?: Array<{
    action: string;
    description?: string;
  }>;
}

export interface DependencyOutputData {
  node_id: string;
  summary?: string;
  status?: string;
  files_changed?: string[];
  artifact_names?: string[];
  artifact_refs?: string[];
}

export interface SelectedReferenceData {
  path: string;
  source?: string;
  kind?: string;
  reason?: string;
}

export interface ContextEnvelopeData {
  schema_version?: number;
  envelope_id?: string;
  task_id?: string;
  session_id?: string | null;
  run_id?: string | null;
  node_id?: string | null;
  dispatch_role?: string;
  logical_attempt?: number;
  assembled_at?: string;
  context_content_digest?: string;
  evidence_digest?: string;
  objective?: string;
  acceptance_criteria?: string[];
  verification_plan?: string[];
  assumptions?: string[];
  non_goals?: string[];
  repo_facts?: RepoFactsData;
  repo_instructions_snapshot?: string | null;
  repo_skills?: RepoSkillData[];
  gated_memory_entries?: GatedMemoryEntryData[];
  gate_diagnostics_summary?: Record<string, unknown>;
  session_context?: SessionContextData;
  capability_summary?: CapabilitySummaryData;
  dependency_outputs?: DependencyOutputData[];
  selected_references?: SelectedReferenceData[];
  truncations?: TruncationRecordData[];
  truncation_records?: TruncationRecordData[];
}

export interface ArtifactItem {
  key?: string;
  name?: string;
  type?: string;
  artifact_type?: string;
  uri?: string;
  metadata?: Record<string, unknown> | null;
  artifact_metadata?: Record<string, unknown> | null;
}

export interface ContextEnvelopeSectionProps {
  artifacts?: ArtifactItem[];
}

function renderJson(val: unknown) {
  if (val == null) return null;
  return (
    <pre className="task-detail-json">
      {typeof val === 'string' ? val : JSON.stringify(val, null, 2)}
    </pre>
  );
}

export function ContextEnvelopeSection({ artifacts = [] }: ContextEnvelopeSectionProps) {
  const envelopes = React.useMemo(() => {
    const list: ContextEnvelopeData[] = [];
    for (const a of artifacts) {
      if (a.type === 'context_envelope' || a.artifact_type === 'context_envelope') {
        const meta = (a.metadata || a.artifact_metadata) as ContextEnvelopeData | null;
        if (meta && typeof meta === 'object') {
          list.push(meta);
        }
      }
    }
    return list;
  }, [artifacts]);

  const grouped = React.useMemo(() => {
    const map: Record<string, ContextEnvelopeData[]> = {};
    for (const env of envelopes) {
      const key = env.node_id && env.node_id.trim() ? env.node_id.trim() : 'primary';
      if (!map[key]) {
        map[key] = [];
      }
      map[key].push(env);
    }
    for (const key of Object.keys(map)) {
      map[key].sort((a, b) => (a.logical_attempt ?? 1) - (b.logical_attempt ?? 1));
    }
    return map;
  }, [envelopes]);

  const groupKeys = React.useMemo(() => {
    const keys = Object.keys(grouped);
    return keys.sort((a, b) => {
      if (a === 'primary') return -1;
      if (b === 'primary') return 1;
      return a.localeCompare(b);
    });
  }, [grouped]);

  if (envelopes.length === 0) {
    return null;
  }

  return (
    <section className="task-detail-section context-envelope-container" aria-label="Context Envelopes">
      <div className="context-envelope-title-row">
        <h4 style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', margin: 0 }}>
          <Shield size={18} className="text-primary" />
          Dispatch Context Envelopes
        </h4>
        <span className="badge badge-neutral" data-testid="envelope-count-badge">
          {envelopes.length} {envelopes.length === 1 ? 'envelope' : 'envelopes'}
        </span>
      </div>

      <div className="context-envelope-groups">
        {groupKeys.map((nodeKey) => {
          const groupEnvelopes = grouped[nodeKey];
          const isPrimary = nodeKey === 'primary';
          return (
            <div key={nodeKey} className="context-envelope-group" data-testid={`envelope-group-${nodeKey}`}>
              <h5 className="context-envelope-group-title">
                <Layers size={14} />
                {isPrimary ? 'Primary Task Dispatch' : `DAG Node: ${nodeKey}`}
              </h5>

              <div className="context-envelope-cards">
                {groupEnvelopes.map((env, idx) => {
                  const attempt = env.logical_attempt ?? idx + 1;
                  const role = env.dispatch_role || 'worker';
                  const contentDigest = env.context_content_digest || '';
                  const evidenceDigest = env.evidence_digest || '';
                  const truncations = env.truncations || env.truncation_records;
                  const schemaVersion = env.schema_version ?? 1;

                  if (schemaVersion !== 1) {
                    return (
                      <article
                        key={env.envelope_id || `${nodeKey}-${attempt}-${idx}`}
                        className="context-envelope-card"
                        data-testid={`envelope-card-${nodeKey}-${attempt}`}
                      >
                        <header className="context-envelope-card-header">
                          <div className="context-envelope-card-badges">
                            <span className="badge badge-attempt">Attempt {attempt}</span>
                            <span className="badge badge-neutral">Schema v{schemaVersion}</span>
                          </div>
                        </header>
                        <div
                          className="context-envelope-truncation-alert"
                          data-testid="unsupported-envelope-version"
                        >
                          <AlertTriangle size={15} />
                          <div>
                            <strong>Unsupported ContextEnvelope schema version.</strong>
                            <p>
                              This dashboard renders structured fields for schema v1 only. Raw
                              metadata is shown below without interpretation.
                            </p>
                            {renderJson(env)}
                          </div>
                        </div>
                      </article>
                    );
                  }

                  return (
                    <article
                      key={env.envelope_id || `${nodeKey}-${attempt}-${idx}`}
                      className="context-envelope-card"
                      data-testid={`envelope-card-${nodeKey}-${attempt}`}
                    >
                      <header className="context-envelope-card-header">
                        <div className="context-envelope-card-badges">
                          <span className="badge badge-attempt" title={`Logical Attempt ${attempt}`}>
                            Attempt {attempt}
                          </span>
                          <span className="badge badge-role" title={`Role: ${role}`}>
                            {role}
                          </span>
                          <span className="badge badge-neutral">Schema v{schemaVersion}</span>
                          {contentDigest && (
                            <span
                              className="badge badge-digest"
                              title={`Context Content Digest (semantic reproducibility): ${contentDigest}`}
                              data-testid="content-digest-badge"
                            >
                              <Hash size={12} />
                              {contentDigest.slice(0, 10)}...
                            </span>
                          )}
                          {evidenceDigest && (
                            <span
                              className="badge badge-evidence"
                              title={`Evidence Digest (audit lineage): ${evidenceDigest}`}
                              data-testid="evidence-digest-badge"
                            >
                              <Lock size={12} />
                              {evidenceDigest.slice(0, 10)}...
                            </span>
                          )}
                        </div>
                        {env.assembled_at && (
                          <time className="context-envelope-time" dateTime={env.assembled_at}>
                            {new Date(env.assembled_at).toLocaleTimeString()}
                          </time>
                        )}
                      </header>

                      {/* Truncation Warnings */}
                      {truncations && truncations.length > 0 && (
                        <div
                          className="context-envelope-truncation-alert"
                          data-testid="truncation-warnings"
                        >
                          <AlertTriangle size={15} />
                          <div>
                            <strong>Context Truncation Applied:</strong>
                            <ul>
                              {truncations.map((rec, rIdx) => {
                                const field = rec.field || rec.section || 'field';
                                const orig = rec.original_length ?? rec.original_count ?? 0;
                                const trunc = rec.truncated_length ?? rec.retained_count ?? 0;
                                const reason = rec.reason || 'exceeded limit';
                                const unit = rec.unit || (rec.original_count != null ? 'items' : 'units');
                                return (
                                  <li key={rIdx}>
                                    <code>{field}</code>: retained {trunc} of{' '}
                                    {orig} {unit} ({reason})
                                  </li>
                                );
                              })}
                            </ul>
                          </div>
                        </div>
                      )}

                      {/* 1. Objective & Intent */}
                      <details className="context-envelope-details" open>
                        <summary>Intent & Verification Plan</summary>
                        <div className="context-envelope-details-content">
                          <p className="context-envelope-objective">
                            <strong>Objective:</strong> {env.objective || 'No objective specified'}
                          </p>

                          {env.acceptance_criteria && env.acceptance_criteria.length > 0 && (
                            <div className="context-envelope-subgroup">
                              <h6>Acceptance Criteria</h6>
                              <ul>
                                {env.acceptance_criteria.map((ac, acIdx) => (
                                  <li key={acIdx}>{ac}</li>
                                ))}
                              </ul>
                            </div>
                          )}

                          {env.verification_plan && env.verification_plan.length > 0 && (
                            <div className="context-envelope-subgroup">
                              <h6>Verification Plan</h6>
                              <ul>
                                {env.verification_plan.map((vp, vpIdx) => (
                                  <li key={vpIdx}>
                                    <code>{vp}</code>
                                  </li>
                                ))}
                              </ul>
                            </div>
                          )}

                          {env.assumptions && env.assumptions.length > 0 && (
                            <div className="context-envelope-subgroup">
                              <h6>Assumptions</h6>
                              <ul>
                                {env.assumptions.map((as, asIdx) => (
                                  <li key={asIdx}>{as}</li>
                                ))}
                              </ul>
                            </div>
                          )}

                          {env.non_goals && env.non_goals.length > 0 && (
                            <div className="context-envelope-subgroup">
                              <h6>Non-Goals</h6>
                              <ul>
                                {env.non_goals.map((ng, ngIdx) => (
                                  <li key={ngIdx}>{ng}</li>
                                ))}
                              </ul>
                            </div>
                          )}
                        </div>
                      </details>

                      {/* 2. Repo Facts & Skills */}
                      <details className="context-envelope-details">
                        <summary>Repository Facts & Guidance</summary>
                        <div className="context-envelope-details-content">
                          <div className="context-envelope-grid-meta">
                            <div>
                              <span className="context-envelope-meta-label">Workspace:</span>
                              <code>{env.repo_facts?.workspace_mode || env.repo_facts?.workspace_type || 'unknown'}</code>
                            </div>
                            {env.repo_facts?.branch && (
                              <div>
                                <span className="context-envelope-meta-label">Branch:</span>
                                <code>{env.repo_facts.branch}</code>
                              </div>
                            )}
                            <div>
                              <span className="context-envelope-meta-label">AGENTS.md:</span>
                              <span>{env.repo_facts?.has_agents_md ? 'Present' : 'Not found'}</span>
                            </div>
                            {(env.repo_facts?.commit_sha || env.repo_facts?.git_head_sha) && (
                              <div>
                                <span className="context-envelope-meta-label">Git HEAD:</span>
                                <code>{(env.repo_facts.commit_sha || env.repo_facts.git_head_sha)!.slice(0, 8)}</code>
                              </div>
                            )}
                            {env.repo_facts?.worktree_state_digest && (
                              <div>
                                <span className="context-envelope-meta-label">Worktree:</span>
                                <code>{env.repo_facts.worktree_state_digest.slice(0, 8)}</code>
                              </div>
                            )}
                            {env.repo_facts?.git_evidence_status && (
                              <div>
                                <span className="context-envelope-meta-label">Git evidence:</span>
                                <span
                                  title={env.repo_facts.git_evidence_reason || undefined}
                                  className={
                                    env.repo_facts.git_evidence_status === 'complete'
                                      ? 'badge badge-neutral'
                                      : 'badge badge-warning'
                                  }
                                >
                                  {env.repo_facts.git_evidence_status}
                                </span>
                              </div>
                            )}
                          </div>

                          {env.repo_facts?.detected_build_systems &&
                            env.repo_facts.detected_build_systems.length > 0 && (
                              <div className="context-envelope-subgroup">
                                <h6>Detected Build Systems</h6>
                                <div className="context-envelope-tags">
                                  {env.repo_facts.detected_build_systems.map((sys) => (
                                    <span key={sys} className="badge badge-neutral">
                                      {sys}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}

                          {env.repo_skills && env.repo_skills.length > 0 && (
                            <div className="context-envelope-subgroup">
                              <h6>Discovered Repository Skills ({env.repo_skills.length})</h6>
                              <ul className="context-envelope-skills-list">
                                {env.repo_skills.map((skill) => (
                                  <li key={skill.relative_path || skill.path || skill.name}>
                                    <strong>{skill.name}</strong>
                                    {skill.description && <span> — {skill.description}</span>}
                                  </li>
                                ))}
                              </ul>
                            </div>
                          )}

                          {env.repo_instructions_snapshot && (
                            <div className="context-envelope-subgroup">
                              <h6>Instructions Snapshot</h6>
                              <pre className="task-detail-json context-envelope-instructions">
                                {env.repo_instructions_snapshot}
                              </pre>
                            </div>
                          )}
                        </div>
                      </details>

                      {/* 3. Gated Memory */}
                      <details className="context-envelope-details">
                        <summary>
                          Gated Memory Entries (
                          {env.gated_memory_entries ? env.gated_memory_entries.length : 0})
                        </summary>
                        <div className="context-envelope-details-content">
                          {env.gated_memory_entries && env.gated_memory_entries.length > 0 ? (
                            <ul className="context-envelope-memory-list">
                              {env.gated_memory_entries.map((entry, mIdx) => (
                                <li key={mIdx} className="context-envelope-memory-item">
                                  <div className="context-envelope-memory-header">
                                    <Brain size={14} />
                                    <strong>{entry.memory_key}</strong>
                                    <span className="badge badge-neutral">
                                      {entry.gate_status || 'accepted'}
                                    </span>
                                    {typeof entry.confidence === 'number' && (
                                      <span className="badge badge-neutral">
                                        {(entry.confidence * 100).toFixed(0)}% conf
                                      </span>
                                    )}
                                  </div>
                                  {renderJson(entry.value)}
                                </li>
                              ))}
                            </ul>
                          ) : (
                            <p className="task-detail-muted">No gated memory entries admitted.</p>
                          )}
                        </div>
                      </details>

                      {/* 4. Session Context */}
                      <details className="context-envelope-details">
                        <summary>Session Context</summary>
                        <div className="context-envelope-details-content">
                          {env.session_context?.active_goal && (
                            <p>
                              <strong>Active Goal:</strong> {env.session_context.active_goal}
                            </p>
                          )}
                          {env.session_context?.files_touched &&
                            env.session_context.files_touched.length > 0 && (
                              <div className="context-envelope-subgroup">
                                <h6>Files Touched in Session</h6>
                                <ul>
                                  {env.session_context.files_touched.map((file) => (
                                    <li key={file}>
                                      <code>{file}</code>
                                    </li>
                                  ))}
                                </ul>
                              </div>
                            )}
                          {Boolean(env.session_context?.decisions_made) && (
                            <div className="context-envelope-subgroup">
                              <h6>Decisions Made</h6>
                              {renderJson(env.session_context?.decisions_made)}
                            </div>
                          )}
                          {Boolean(env.session_context?.identified_risks) && (
                            <div className="context-envelope-subgroup">
                              <h6>Identified Risks</h6>
                              {renderJson(env.session_context?.identified_risks)}
                            </div>
                          )}
                        </div>
                      </details>

                      {/* 5. Capabilities */}
                      <details className="context-envelope-details">
                        <summary>Capabilities & Permissions</summary>
                        <div className="context-envelope-details-content">
                          <div className="context-envelope-tags" style={{ marginBottom: '0.5rem' }}>
                            <span className="badge badge-neutral">
                              Network: {env.capability_summary?.network_enabled ? 'Allowed' : 'Disabled'}
                            </span>
                            <span className="badge badge-neutral">
                              Read-Only: {env.capability_summary?.read_only ? 'True' : 'False'}
                            </span>
                            {env.capability_summary?.risk_level && (
                              <span className="badge badge-neutral">
                                Risk: {env.capability_summary.risk_level}
                              </span>
                            )}
                            {env.capability_summary?.delivery_mode && (
                              <span className="badge badge-neutral">
                                Delivery: {env.capability_summary.delivery_mode}
                              </span>
                            )}
                          </div>

                          {env.capability_summary?.allowed_actions &&
                            env.capability_summary.allowed_actions.length > 0 && (
                              <div className="context-envelope-subgroup">
                                <h6>Allowed Actions</h6>
                                <div className="context-envelope-tags">
                                  {env.capability_summary.allowed_actions.map((act) => (
                                    <span key={act} className="badge badge-neutral">
                                      {act}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}

                          {env.capability_summary?.forbidden_actions &&
                            env.capability_summary.forbidden_actions.length > 0 && (
                              <div className="context-envelope-subgroup">
                                <h6>Forbidden Actions</h6>
                                <div className="context-envelope-tags">
                                  {env.capability_summary.forbidden_actions.map((act) => (
                                    <span key={act} className="badge badge-neutral">
                                      {act}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}

                          {env.capability_summary?.granted_secret_refs &&
                            env.capability_summary.granted_secret_refs.length > 0 && (
                              <div className="context-envelope-subgroup">
                                <h6>Granted Secret References</h6>
                                <div className="context-envelope-tags">
                                  {env.capability_summary.granted_secret_refs.map((ref) => (
                                    <span key={ref} className="badge badge-neutral">
                                      <Lock size={10} /> {ref}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}

                          {env.capability_summary?.granted_tools &&
                            env.capability_summary.granted_tools.length > 0 && (
                              <div className="context-envelope-subgroup">
                                <h6>Granted Tools</h6>
                                <div className="context-envelope-tags">
                                  {env.capability_summary.granted_tools.map((tool) => (
                                    <span key={tool} className="badge badge-neutral">
                                      {tool}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}

                          {env.capability_summary?.approval_capabilities &&
                            env.capability_summary.approval_capabilities.length > 0 && (
                              <div className="context-envelope-subgroup">
                                <h6>Approval Capabilities</h6>
                                <div className="context-envelope-tags">
                                  {env.capability_summary.approval_capabilities.map((cap) => (
                                    <span key={cap} className="badge badge-neutral">
                                      {cap}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}
                        </div>
                      </details>

                      {/* 6. Upstream Dependencies */}
                      {env.dependency_outputs && env.dependency_outputs.length > 0 && (
                        <details className="context-envelope-details">
                          <summary>Upstream Node Outputs ({env.dependency_outputs.length})</summary>
                          <div className="context-envelope-details-content">
                            {env.dependency_outputs.map((dep) => {
                              const artNames = dep.artifact_names || dep.artifact_refs;
                              return (
                                <div key={dep.node_id} className="context-envelope-dep-card">
                                  <div className="context-envelope-dep-header">
                                    <strong>Node: {dep.node_id}</strong>
                                    <span className="badge badge-neutral">{dep.status || 'completed'}</span>
                                  </div>
                                  {dep.summary && <p className="context-envelope-dep-summary">{dep.summary}</p>}
                                  {dep.files_changed && dep.files_changed.length > 0 && (
                                    <div className="context-envelope-subgroup">
                                      <h6>Files Changed</h6>
                                      <ul>
                                        {dep.files_changed.map((f) => (
                                          <li key={f}>
                                            <code>{f}</code>
                                          </li>
                                        ))}
                                      </ul>
                                    </div>
                                  )}
                                  {artNames && artNames.length > 0 && (
                                    <div className="context-envelope-subgroup">
                                      <h6>Artifacts</h6>
                                      <div className="context-envelope-tags">
                                        {artNames.map((art) => (
                                          <span key={art} className="badge badge-neutral">
                                            {art}
                                          </span>
                                        ))}
                                      </div>
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        </details>
                      )}

                      {/* 7. Selected References */}
                      {env.selected_references && env.selected_references.length > 0 && (
                        <details className="context-envelope-details">
                          <summary>Selected References ({env.selected_references.length})</summary>
                          <div className="context-envelope-details-content">
                            <ul className="context-envelope-skills-list">
                              {env.selected_references.map((ref, rIdx) => {
                                const kindOrSource = ref.source || ref.kind;
                                return (
                                  <li key={rIdx}>
                                    <FileCode size={13} style={{ display: 'inline', marginRight: '4px' }} />
                                    <code>{ref.path}</code>
                                    {kindOrSource && (
                                      <span className="task-detail-muted"> ({kindOrSource})</span>
                                    )}
                                    {ref.reason && <span> — {ref.reason}</span>}
                                  </li>
                                );
                              })}
                            </ul>
                          </div>
                        </details>
                      )}
                    </article>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
