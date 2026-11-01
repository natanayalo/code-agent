#!/bin/sh
# init_webhook.sh — One-time setup for Telegram webhook in Docker.

set -eu

enabled=$(printf '%s' "${CODE_AGENT_TELEGRAM_WEBHOOK_ENABLED:-0}" \
  | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')
case "$enabled" in
  0|false|no|off) echo "Telegram webhook disabled; registration skipped."; exit 0 ;;
  1|true|yes|on) ;;
  *) echo "Invalid Telegram enablement setting." >&2; exit 1 ;;
esac
fail() { echo "Telegram webhook registration: $1" >&2; exit 1; }
secret=${CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN:-}
if [ "${#secret}" -lt 32 ] || [ "${#secret}" -gt 256 ]; then
  fail "invalid secret length"
fi
case "$secret" in *[!a-zA-Z0-9_-]*) fail "invalid secret format" ;; esac
[ -n "${CODE_AGENT_TELEGRAM_BOT_TOKEN:-}" ] || fail "bot token is required"
printf '%s\n' "$CODE_AGENT_TELEGRAM_BOT_TOKEN" | grep -Eq '^[0-9]+:[a-zA-Z0-9_-]+$' \
  || fail "invalid bot token format"
for ids in "${CODE_AGENT_TELEGRAM_ALLOWED_USER_IDS:-}" "${CODE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS:-}"; do
  printf '%s\n' "$ids" | grep -Eq '^[[:space:]]*[1-9][0-9]{0,15}[[:space:]]*(,[[:space:]]*[1-9][0-9]{0,15}[[:space:]]*)*$' \
    || fail "numeric user and private-chat allowlists are required"
done

# Install dependencies
apk add --no-cache curl jq >/dev/null
umask 077
payload=$(mktemp)
response=$(mktemp)
trap 'rm -f "$payload" "$response"' EXIT HUP INT TERM

echo "Waiting for ngrok URL..."
PUBLIC_URL=""
for i in $(seq 1 30); do
  # Fetch public URL from ngrok API
  PUBLIC_URL=$(curl -fsS --max-time 5 http://ngrok:4040/api/tunnels 2>/dev/null \
    | jq -r '[.tunnels[] | select(.proto=="https") | .public_url][0] // empty') || PUBLIC_URL=""

  if [ -n "$PUBLIC_URL" ] && [ "$PUBLIC_URL" != "null" ]; then
    break
  fi
  echo "Still waiting for ngrok... ($i/30)"
  sleep 2
done

if [ -z "$PUBLIC_URL" ] || [ "$PUBLIC_URL" = "null" ]; then
  echo "❌ Could not find ngrok URL after 60 seconds."
  exit 1
fi

case "$PUBLIC_URL" in https://*) ;; *) fail "HTTPS tunnel was not found" ;; esac

if [ -z "$CODE_AGENT_TELEGRAM_BOT_TOKEN" ]; then
  echo "⚠️  CODE_AGENT_TELEGRAM_BOT_TOKEN is not set. Skipping webhook registration."
  exit 0
fi

echo "Registering Telegram webhook..."
# Values are read from the environment; secrets never appear in process arguments.
export M2_TELEGRAM_WEBHOOK_URL="${PUBLIC_URL}/telegram/webhook"
jq -n '{url: env.M2_TELEGRAM_WEBHOOK_URL,
  secret_token: env.CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN,
  allowed_updates: ["message"], drop_pending_updates: false}' > "$payload"
printf 'url = "https://api.telegram.org/bot%s/setWebhook"\n' "$CODE_AGENT_TELEGRAM_BOT_TOKEN" \
  | curl -fs --max-time 30 --config - -X POST -H 'Content-Type: application/json' \
      --data-binary "@$payload" -o "$response" 2>/dev/null \
  || fail "Telegram request failed (response suppressed)"
jq -e '.ok == true and .result == true' "$response" >/dev/null \
  || fail "Telegram rejected registration (response suppressed)"
echo "Telegram webhook registered; pending updates preserved."
