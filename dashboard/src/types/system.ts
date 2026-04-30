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
