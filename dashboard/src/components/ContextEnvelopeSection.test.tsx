import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import {
  ContextEnvelopeSection,
  ContextEnvelopeData,
  ArtifactItem,
} from './ContextEnvelopeSection';

describe('ContextEnvelopeSection', () => {
  it('renders null when artifacts array is empty or has no context envelopes', () => {
    const { container: c1 } = render(<ContextEnvelopeSection artifacts={[]} />);
    expect(c1.firstChild).toBeNull();

    const otherArtifacts: ArtifactItem[] = [
      {
        key: 'patch-1',
        name: 'changes.patch',
        type: 'patch',
        uri: 'file:///tmp/changes.patch',
      },
    ];
    const { container: c2 } = render(<ContextEnvelopeSection artifacts={otherArtifacts} />);
    expect(c2.firstChild).toBeNull();
  });

  it('renders a primary context envelope with badges, intent, repo facts, and memory', () => {
    const envelope: ContextEnvelopeData = {
      schema_version: 1,
      envelope_id: 'env-1',
      task_id: 'task-100',
      session_id: 'sess-100',
      node_id: null,
      dispatch_role: 'worker',
      logical_attempt: 1,
      assembled_at: '2026-09-13T10:00:00.000Z',
      context_content_digest: 'sha256:abc123semanticdigest',
      evidence_digest: 'sha256:def456evidencedigest',
      objective: 'Implement feature Z',
      acceptance_criteria: ['AC1: Tests pass', 'AC2: Docs updated'],
      verification_plan: ['pytest tests/'],
      assumptions: ['Python 3.12+ available'],
      non_goals: ['No breaking API changes'],
      repo_facts: {
        workspace_type: 'clone',
        has_agents_md: true,
        git_head_sha: 'a1b2c3d4e5f6',
        detected_build_systems: ['poetry/pyproject', 'docker'],
      },
      repo_skills: [
        {
          name: 'DB Schema',
          description: 'Guidance for migrations',
          path: '.agents/skills/db-schema/SKILL.md',
        },
      ],
      repo_instructions_snapshot: '# Project Rules\nFollow guidelines.',
      gated_memory_entries: [
        {
          memory_key: 'arch_style',
          value: { pattern: 'event_driven' },
          confidence: 0.92,
          gate_status: 'accepted',
        },
      ],
      session_context: {
        active_goal: 'Complete milestone',
        files_touched: ['db/models.py'],
        decisions_made: { engine: 'sqlite' },
        identified_risks: { perf: 'low' },
      },
      capability_summary: {
        granted_tools: ['read_file', 'write_file'],
        granted_secret_refs: ['GITHUB_TOKEN'],
        network_enabled: false,
        read_only: false,
        approval_capabilities: ['clarification'],
      },
    };

    const artifacts: ArtifactItem[] = [
      {
        key: 'env-art-1',
        name: 'context_envelope',
        type: 'context_envelope',
        uri: 'context_envelope://task-100/attempt-1',
        metadata: envelope as unknown as Record<string, unknown>,
      },
    ];

    render(<ContextEnvelopeSection artifacts={artifacts} />);

    // Section title & count
    expect(screen.getByText('Dispatch Context Envelopes')).toBeInTheDocument();
    expect(screen.getByTestId('envelope-count-badge')).toHaveTextContent('1 envelope');

    // Group title
    expect(screen.getByText('Primary Task Dispatch')).toBeInTheDocument();

    // Badges
    expect(screen.getByText('Attempt 1')).toBeInTheDocument();
    expect(screen.getByText('worker')).toBeInTheDocument();
    expect(screen.getByTestId('content-digest-badge')).toHaveTextContent('sha256:abc...');
    expect(screen.getByTestId('evidence-digest-badge')).toHaveTextContent('sha256:def...');

    // Objective & criteria
    expect(screen.getByText(/Implement feature Z/)).toBeInTheDocument();
    expect(screen.getByText('AC1: Tests pass')).toBeInTheDocument();
    expect(screen.getByText('AC2: Docs updated')).toBeInTheDocument();
    expect(screen.getByText('pytest tests/')).toBeInTheDocument();
    expect(screen.getByText('Python 3.12+ available')).toBeInTheDocument();
    expect(screen.getByText('No breaking API changes')).toBeInTheDocument();

    // Repo facts
    expect(screen.getByText('poetry/pyproject')).toBeInTheDocument();
    expect(screen.getByText('docker')).toBeInTheDocument();
    expect(screen.getByText('DB Schema')).toBeInTheDocument();
    expect(screen.getByText(/# Project Rules/)).toBeInTheDocument();

    // Gated Memory
    expect(screen.getByText('arch_style')).toBeInTheDocument();
    expect(screen.getByText('92% conf')).toBeInTheDocument();

    // Session Context
    expect(screen.getByText('Complete milestone')).toBeInTheDocument();
    expect(screen.getByText('db/models.py')).toBeInTheDocument();

    // Capabilities
    expect(screen.getByText('Network: Disabled')).toBeInTheDocument();
    expect(screen.getByText('Read-Only: False')).toBeInTheDocument();
    expect(screen.getByText('GITHUB_TOKEN')).toBeInTheDocument();
    expect(screen.getByText('read_file')).toBeInTheDocument();
  });

  it('groups multiple envelopes by node_id and sorts by logical_attempt ascending', () => {
    const envPrimaryAttempt2: ContextEnvelopeData = {
      envelope_id: 'env-p2',
      node_id: null,
      dispatch_role: 'worker_repair',
      logical_attempt: 2,
      objective: 'Repair primary task',
      context_content_digest: 'sha256:p2',
    };
    const envPrimaryAttempt1: ContextEnvelopeData = {
      envelope_id: 'env-p1',
      node_id: null,
      dispatch_role: 'worker',
      logical_attempt: 1,
      objective: 'Initial primary task',
      context_content_digest: 'sha256:p1',
    };
    const envNodeA: ContextEnvelopeData = {
      envelope_id: 'env-na',
      node_id: 'node-A',
      dispatch_role: 'decomposed_node',
      logical_attempt: 1,
      objective: 'Execute DAG Node A',
      context_content_digest: 'sha256:na',
    };

    const artifacts: ArtifactItem[] = [
      // Deliberately unsorted
      {
        type: 'context_envelope',
        artifact_metadata: envNodeA as unknown as Record<string, unknown>,
      },
      {
        type: 'context_envelope',
        artifact_metadata: envPrimaryAttempt2 as unknown as Record<string, unknown>,
      },
      {
        type: 'context_envelope',
        artifact_metadata: envPrimaryAttempt1 as unknown as Record<string, unknown>,
      },
    ];

    render(<ContextEnvelopeSection artifacts={artifacts} />);

    expect(screen.getByTestId('envelope-count-badge')).toHaveTextContent('3 envelopes');

    // Groups check: primary group and node-A group
    const primaryGroup = screen.getByTestId('envelope-group-primary');
    const nodeAGroup = screen.getByTestId('envelope-group-node-A');
    expect(primaryGroup).toBeInTheDocument();
    expect(nodeAGroup).toBeInTheDocument();

    // Within primary group: attempt 1 card comes before attempt 2 card
    const cardAttempt1 = screen.getByTestId('envelope-card-primary-1');
    const cardAttempt2 = screen.getByTestId('envelope-card-primary-2');
    expect(primaryGroup.compareDocumentPosition(cardAttempt1) & Node.DOCUMENT_POSITION_CONTAINED_BY).toBeTruthy();
    expect(cardAttempt1.compareDocumentPosition(cardAttempt2) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('renders truncation warnings prominently when truncation_records are present', () => {
    const envelope: ContextEnvelopeData = {
      envelope_id: 'env-trunc',
      logical_attempt: 1,
      objective: 'Task with truncated context',
      truncation_records: [
        {
          section: 'repo_skills',
          original_count: 50,
          retained_count: 10,
          reason: 'MAX_REPO_SKILLS limit of 10 reached',
        },
      ],
    };

    render(
      <ContextEnvelopeSection
        artifacts={[
          {
            type: 'context_envelope',
            metadata: envelope as unknown as Record<string, unknown>,
          },
        ]}
      />
    );

    const alert = screen.getByTestId('truncation-warnings');
    expect(alert).toBeInTheDocument();
    expect(alert).toHaveTextContent('Context Truncation Applied:');
    expect(alert).toHaveTextContent('repo_skills');
    expect(alert).toHaveTextContent('retained 10 of 50 items');
    expect(alert).toHaveTextContent('MAX_REPO_SKILLS limit of 10 reached');
  });

  it('renders upstream dependencies and selected references', () => {
    const envelope: ContextEnvelopeData = {
      envelope_id: 'env-deps',
      node_id: 'node-final',
      logical_attempt: 1,
      objective: 'Aggregate outputs',
      dependency_outputs: [
        {
          node_id: 'node-prep',
          status: 'completed',
          summary: 'Prepared environment',
          files_changed: ['env.sh'],
        },
      ],
      selected_references: [
        {
          kind: 'file',
          path: 'src/config.ts',
          reason: 'Contains DB config',
        },
      ],
    };

    render(
      <ContextEnvelopeSection
        artifacts={[
          {
            type: 'context_envelope',
            metadata: envelope as unknown as Record<string, unknown>,
          },
        ]}
      />
    );

    expect(screen.getByText('Upstream Node Outputs (1)')).toBeInTheDocument();
    expect(screen.getByText('Node: node-prep')).toBeInTheDocument();
    expect(screen.getByText('Prepared environment')).toBeInTheDocument();
    expect(screen.getByText('env.sh')).toBeInTheDocument();

    expect(screen.getByText('Selected References (1)')).toBeInTheDocument();
    expect(screen.getByText('src/config.ts')).toBeInTheDocument();
    expect(screen.getByText(/Contains DB config/)).toBeInTheDocument();
  });

  it('correctly handles group sorting when primary is encountered after DAG nodes', () => {
    const artifacts: ArtifactItem[] = [
      {
        type: 'context_envelope',
        metadata: {
          node_id: 'node-Z',
          logical_attempt: 1,
          objective: 'Z node',
        },
      },
      {
        type: 'context_envelope',
        metadata: {
          node_id: 'node-A',
          logical_attempt: 1,
          objective: 'A node',
        },
      },
      {
        type: 'context_envelope',
        metadata: {
          node_id: null,
          logical_attempt: 1,
          objective: 'Primary node',
        },
      },
    ];

    render(<ContextEnvelopeSection artifacts={artifacts} />);

    const primaryGroup = screen.getByTestId('envelope-group-primary');
    const nodeAGroup = screen.getByTestId('envelope-group-node-A');
    const nodeZGroup = screen.getByTestId('envelope-group-node-Z');

    expect(primaryGroup.compareDocumentPosition(nodeAGroup) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(nodeAGroup.compareDocumentPosition(nodeZGroup) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('renders minimal envelope with fallback strings and empty states', () => {
    const envelope: ContextEnvelopeData = {
      envelope_id: 'env-minimal',
      logical_attempt: undefined,
      objective: undefined,
      repo_facts: undefined,
      gated_memory_entries: [],
      session_context: undefined,
      capability_summary: {
        approval_capabilities: [],
        granted_secret_refs: [],
        granted_tools: [],
      },
    };

    render(
      <ContextEnvelopeSection
        artifacts={[
          {
            type: 'context_envelope',
            metadata: envelope as unknown as Record<string, unknown>,
          },
        ]}
      />
    );

    expect(screen.getByText('Attempt 1')).toBeInTheDocument();
    expect(screen.getByText('No objective specified')).toBeInTheDocument();
    expect(screen.getByText('No gated memory entries admitted.')).toBeInTheDocument();
  });

  it('renders exact backend-shaped serialized ContextEnvelope schema', () => {
    const backendEnvelope: ContextEnvelopeData = {
      schema_version: 1,
      envelope_id: 'env-backend-1',
      task_id: 'task-backend-100',
      session_id: 'sess-backend-100',
      node_id: null,
      dispatch_role: 'primary',
      logical_attempt: 1,
      assembled_at: '2026-09-13T12:00:00.000Z',
      context_content_digest: 'sha256:11223344556677889900aabbccddeeff',
      evidence_digest: 'sha256:99887766554433221100ffeeddccbbaa',
      objective: 'Backend aligned envelope test',
      acceptance_criteria: ['Criterion A', 'Criterion B'],
      assumptions: ['Assumption 1'],
      non_goals: ['Non-goal 1'],
      verification_plan: ['pytest tests/unit'],
      repo_facts: {
        repo_url: 'https://github.com/example/repo.git',
        branch: 'feat/backend-test',
        workspace_mode: 'isolated_workspace',
        workspace_id: 'ws-backend-100',
        has_agents_md: true,
        detected_build_systems: ['poetry'],
        workspace_identity_omission_reason: 'orchestrator_dispatch_boundary',
      },
      repo_skills: [
        {
          name: 'backend-skill',
          description: 'A backend skill',
          relative_path: '.agents/skills/backend/SKILL.md',
        },
      ],
      gated_memory_entries: [
        {
          memory_key: 'project_convention',
          value: { style: 'typed' },
          category: 'project',
          confidence: 0.95,
          gate_status: 'accepted',
        },
      ],
      session_context: {
        active_goal: 'Test backend schema',
        files_touched: ['orchestrator/context_envelope.py'],
        decisions_made: { schema: 'aligned' },
        identified_risks: { regressions: 'none' },
      },
      capability_summary: {
        read_only: false,
        risk_level: 'low',
        allowed_actions: ['fs_read', 'fs_write'],
        forbidden_actions: ['rm_all'],
        delivery_mode: 'workspace',
        network_enabled: true,
        granted_secret_refs: ['TEST_KEY'],
        worker_type: 'claude',
      },
      dependency_outputs: [
        {
          node_id: 'node-prep',
          status: 'completed',
          summary: 'Deps prepared',
          files_changed: ['poetry.lock'],
          artifact_names: ['build.log', 'spec.json'],
        },
      ],
      selected_references: [
        {
          path: 'orchestrator/graph.py',
          source: 'session_files_touched',
        },
      ],
      truncations: [
        {
          field: 'objective',
          original_length: 5000,
          truncated_length: 4000,
          reason: 'exceeded_limit',
        },
      ],
    };

    render(
      <ContextEnvelopeSection
        artifacts={[
          {
            artifact_type: 'context_envelope',
            artifact_metadata: backendEnvelope as unknown as Record<string, unknown>,
          },
        ]}
      />
    );

    // Repo facts with workspace_mode & branch
    expect(screen.getByText('isolated_workspace')).toBeInTheDocument();
    expect(screen.getByText('feat/backend-test')).toBeInTheDocument();

    // Repo skills with relative_path
    expect(screen.getByText('backend-skill')).toBeInTheDocument();
    expect(screen.getByText(/A backend skill/)).toBeInTheDocument();

    // Capability summary with allowed/forbidden actions, risk, delivery
    expect(screen.getByText('Network: Allowed')).toBeInTheDocument();
    expect(screen.getByText('Risk: low')).toBeInTheDocument();
    expect(screen.getByText('Delivery: workspace')).toBeInTheDocument();
    expect(screen.getByText('fs_read')).toBeInTheDocument();
    expect(screen.getByText('fs_write')).toBeInTheDocument();
    expect(screen.getByText('rm_all')).toBeInTheDocument();

    // Dependency outputs with artifact_names
    expect(screen.getByText('Node: node-prep')).toBeInTheDocument();
    expect(screen.getByText('build.log')).toBeInTheDocument();
    expect(screen.getByText('spec.json')).toBeInTheDocument();

    // Selected references with source
    expect(screen.getByText('orchestrator/graph.py')).toBeInTheDocument();
    expect(screen.getByText('(session_files_touched)')).toBeInTheDocument();

    // Truncations with field, original_length, truncated_length, reason
    const truncAlert = screen.getByTestId('truncation-warnings');
    expect(truncAlert).toHaveTextContent('objective');
    expect(truncAlert).toHaveTextContent('retained 4000 of 5000 items (exceeded_limit)');
  });
});
