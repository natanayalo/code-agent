export interface ToolDefinition {
  name: string;
  description: string;
  capability_category: string;
  side_effect_level: string;
  required_permission: string;
  timeout_seconds: number;
  network_required: boolean;
  expected_artifacts: string[];
  required_secrets: string[];
  deterministic: boolean;
}

export interface SandboxStatusResponse {
  default_image: string;
  workspace_root: string;
}

export interface RuntimeManifest {
  service?: {
    service_name: string;
    schema_version: number;
    environment: string;
    build_sha?: string | null;
  } | null;
  sandbox?: SandboxStatusResponse | null;
  worker?: {
    worker_type?: string | null;
    worker_profile?: string | null;
    runtime_mode?: string | null;
    workspace_id?: string | null;
  } | null;
  task?: {
    read_only: boolean;
    network_enabled: boolean;
    delivery_mode?: string | null;
    budget: Record<string, unknown>;
    allowed_actions: string[];
    forbidden_actions: string[];
    approval_required: boolean;
  } | null;
  tools?: Array<{
    name: string;
    capability_category: string;
    side_effect_level: string;
    required_permission: string;
    network_required: boolean;
    deterministic: boolean;
  }> | null;
  approval_capabilities?: string[] | null;
  maintenance_actions?: Array<{
    action: string;
    description: string;
    request_only: boolean;
    requires_operator_approval: boolean;
  }> | null;
}
