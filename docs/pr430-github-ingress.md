# PR #430 split: github ingress

The API Compose service now receives CODE_AGENT_GITHUB_WEBHOOK_SECRET, CODE_AGENT_GITHUB_OWNER_LOGIN and CODE_AGENT_GITHUB_ISSUE_LABEL (default code-agent). Signed webhook bytes are limited to 1 MiB while streaming, before JSON decoding or full Request.body buffering, regardless of Content-Length. Larger deliveries receive 413. This is an intentional ingress limit added during the PR #430 split; configure the existing secret/allowlists before enabling intake. No public route or tunnel is enabled. Revert the PR to roll back; there is no schema change.
