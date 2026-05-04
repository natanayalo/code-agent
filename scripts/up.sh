#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "[run-production-like][error] Missing $ENV_FILE. Copy .env.example and fill secrets first." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[run-production-like][error] docker is required." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "[run-production-like][error] Docker daemon is not reachable." >&2
  exit 1
fi

is_enabled() {
  local value="${1:-}"
  value="$(echo "$value" | tr '[:upper:]' '[:lower:]')"
  [ "$value" = "1" ] || [ "$value" = "true" ] || [ "$value" = "yes" ] || [ "$value" = "on" ]
}

# shellcheck source=/dev/null
set -a
# shellcheck disable=SC1090,SC1091
source "$ENV_FILE"
set +a

export CODE_AGENT_CODEX_AUTH_DIR="${CODE_AGENT_CODEX_AUTH_DIR:-$HOME/.codex}"
export CODE_AGENT_GEMINI_AUTH_DIR="${CODE_AGENT_GEMINI_AUTH_DIR:-$HOME/.gemini}"
export CODE_AGENT_WORKSPACE_ROOT="${CODE_AGENT_WORKSPACE_ROOT:-$HOME/.code-agent/workspaces}"
export CODE_AGENT_CODEX_SANDBOX="${CODE_AGENT_CODEX_SANDBOX:-workspace-write}"
ALLOW_READ_ONLY_SANDBOX="${CODE_AGENT_ALLOW_READ_ONLY_SANDBOX:-0}"
EXPECTED_HOME="$(eval echo "~$(id -un)")"
if [ -z "$EXPECTED_HOME" ] || [ "$EXPECTED_HOME" = "~$(id -un)" ]; then
  EXPECTED_HOME="$HOME"
fi
FALLBACK_CODEX_AUTH_DIR="$EXPECTED_HOME/.codex"
FALLBACK_GEMINI_AUTH_DIR="$EXPECTED_HOME/.gemini"

if [ ! -f "$CODE_AGENT_CODEX_AUTH_DIR/auth.json" ] && [ -f "$FALLBACK_CODEX_AUTH_DIR/auth.json" ]; then
  echo "[run-production-like][warn] CODE_AGENT_CODEX_AUTH_DIR points to a missing path: $CODE_AGENT_CODEX_AUTH_DIR" >&2
  echo "[run-production-like][warn] Falling back to detected path: $FALLBACK_CODEX_AUTH_DIR" >&2
  export CODE_AGENT_CODEX_AUTH_DIR="$FALLBACK_CODEX_AUTH_DIR"
fi

if [ ! -f "$CODE_AGENT_GEMINI_AUTH_DIR/oauth_creds.json" ] && [ -f "$FALLBACK_GEMINI_AUTH_DIR/oauth_creds.json" ]; then
  echo "[run-production-like][warn] CODE_AGENT_GEMINI_AUTH_DIR points to a missing path: $CODE_AGENT_GEMINI_AUTH_DIR" >&2
  echo "[run-production-like][warn] Falling back to detected path: $FALLBACK_GEMINI_AUTH_DIR" >&2
  export CODE_AGENT_GEMINI_AUTH_DIR="$FALLBACK_GEMINI_AUTH_DIR"
fi

if [ ! -f "$CODE_AGENT_CODEX_AUTH_DIR/auth.json" ]; then
  echo "[run-production-like][error] Codex auth was not found at $CODE_AGENT_CODEX_AUTH_DIR/auth.json" >&2
  echo "[run-production-like][error] Run one-time login in worker container:" >&2
  echo "[run-production-like][error]   docker compose run --rm --no-deps worker codex login" >&2
  echo "[run-production-like][error] Or set CODE_AGENT_CODEX_AUTH_DIR to the directory containing auth.json." >&2
  exit 1
fi

if [ ! -f "$CODE_AGENT_GEMINI_AUTH_DIR/oauth_creds.json" ]; then
  echo "[run-production-like][warn] Gemini auth was not found at $CODE_AGENT_GEMINI_AUTH_DIR/oauth_creds.json" >&2
  echo "[run-production-like][warn] Gemini worker may fail until you run 'gemini auth login' on host." >&2
fi

if [ "$CODE_AGENT_CODEX_SANDBOX" = "read-only" ] && [ "$ALLOW_READ_ONLY_SANDBOX" != "1" ]; then
  echo "[run-production-like][warn] CODE_AGENT_CODEX_SANDBOX=read-only blocks file edits in production-like mode." >&2
  echo "[run-production-like][warn] Overriding to workspace-write. Set CODE_AGENT_ALLOW_READ_ONLY_SANDBOX=1 to keep read-only." >&2
  export CODE_AGENT_CODEX_SANDBOX="workspace-write"
elif [ "$CODE_AGENT_CODEX_SANDBOX" = "read-only" ]; then
  echo "[run-production-like][warn] CODE_AGENT_CODEX_SANDBOX=read-only, so Codex tasks cannot modify files." >&2
fi

mkdir -p "$CODE_AGENT_WORKSPACE_ROOT"
echo "[run-production-like] Using shared workspace root: $CODE_AGENT_WORKSPACE_ROOT"
echo "[run-production-like] Codex sandbox mode: $CODE_AGENT_CODEX_SANDBOX"

if is_enabled "${CODE_AGENT_ENABLE_TRACING:-0}"; then
  echo "[run-production-like] Tracing enabled (CODE_AGENT_ENABLE_TRACING=1); starting phoenix too"
  echo "[run-production-like] Starting postgres + migration + api + worker + phoenix + dashboard"
  docker compose --profile observability --env-file "$ENV_FILE" up -d --build postgres migrate api worker phoenix dashboard
else
  echo "[run-production-like] Starting postgres + migration + api + worker + dashboard"
  docker compose --env-file "$ENV_FILE" up -d --build postgres migrate api worker dashboard
fi

api_container_id="$(docker compose --env-file "$ENV_FILE" ps -q api)"
if [ -z "$api_container_id" ]; then
  echo "[run-production-like][error] API container ID not found after compose up." >&2
  exit 1
fi

echo "[run-production-like] Waiting for API health (docker healthcheck + /health)"
for _ in $(seq 1 90); do
  api_health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$api_container_id" 2>/dev/null || true)"
  if [ "$api_health" = "healthy" ] && curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

api_health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$api_container_id" 2>/dev/null || true)"
if [ "$api_health" != "healthy" ]; then
  echo "[run-production-like][error] API container health is '$api_health' (expected 'healthy')." >&2
  docker compose --env-file "$ENV_FILE" ps >&2
  exit 1
fi
curl -fsS http://127.0.0.1:8000/health >/dev/null
echo "[run-production-like] API is healthy at http://127.0.0.1:8000"
echo "[run-production-like] Dashboard is available at http://localhost:3000"
echo "[run-production-like] Services running:"
docker compose --env-file "$ENV_FILE" ps

echo "[run-production-like] Next: register Telegram webhook to /telegram/webhook if needed."
