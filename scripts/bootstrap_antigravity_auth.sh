#!/usr/bin/env bash
# Enroll Antigravity credentials without exposing them to a task executor.
set -euo pipefail

: "${CODE_AGENT_ANTIGRAVITY_AUTH_DIR:?Set CODE_AGENT_ANTIGRAVITY_AUTH_DIR to an absolute host path.}"

auth_dir="${CODE_AGENT_ANTIGRAVITY_AUTH_DIR}"
mode="${1:-login}"
image="${CODE_AGENT_NATIVE_AGENT_EXECUTOR_IMAGE:-code-agent-worker}"
run_id="${RANDOM}${RANDOM}"
network_name="antigravity-auth-${run_id}"
proxy_name="antigravity-auth-proxy-${run_id}"
logs_dir="$(mktemp -d)"

if [[ "${auth_dir}" != /* ]]; then
  echo "CODE_AGENT_ANTIGRAVITY_AUTH_DIR must be an absolute path." >&2
  exit 2
fi

if [[ "${mode}" != "login" && "${mode}" != "--check" ]]; then
  echo "Usage: $0 [--check]" >&2
  exit 2
fi

mkdir -p "${auth_dir}"

cleanup() {
  docker rm -f "${proxy_name}" >/dev/null 2>&1 || true
  docker network rm "${network_name}" >/dev/null 2>&1 || true
  rm -rf "${logs_dir}"
}
trap cleanup EXIT

docker network create --internal "${network_name}" >/dev/null
docker run -d --rm \
  --name "${proxy_name}" \
  --network "${network_name}" \
  --network-alias native-egress-proxy \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --pids-limit 128 \
  --memory 128m \
  --cpus 0.25 \
  --ipc private \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --mount "type=bind,source=${logs_dir},target=/logs" \
  --env CODE_AGENT_PROXY_AUDIT_PATH=/logs/egress-audit.jsonl \
  --env CODE_AGENT_PROXY_TASK_ID=operator-antigravity-auth \
  "${image}" python /app/sandbox/native_agent_proxy.py >/dev/null
docker network connect bridge "${proxy_name}"

if [[ "${mode}" == "--check" ]]; then
  echo "Checking the enrolled Antigravity credential with a fixed, repository-free prompt."
  command=(agy -p "Reply exactly AUTH_OK")
else
  echo "Complete the Antigravity browser sign-in, then exit the CLI."
  command=(agy)
fi
echo "Only ${auth_dir} is persistent; no task workspace or Docker socket is mounted."
tty_flags=(-i)
if [[ -t 0 && -t 1 ]]; then
  tty_flags=(-it)
fi
docker run --rm "${tty_flags[@]}" \
  --network "${network_name}" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --pids-limit 128 \
  --memory 512m \
  --cpus 1.0 \
  --ipc private \
  --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  --mount "type=bind,source=${auth_dir},target=/tmp/.gemini" \
  --env HOME=/tmp \
  --env GEMINI_HOME=/tmp/.gemini \
  --env HTTP_PROXY=http://native-egress-proxy:8080 \
  --env HTTPS_PROXY=http://native-egress-proxy:8080 \
  --env NO_PROXY= \
  --env AGY_CLI_DISABLE_AUTO_UPDATE=true \
  --workdir /tmp \
  "${image}" "${command[@]}"
