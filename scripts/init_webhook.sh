#!/bin/sh
# init_webhook.sh — One-time setup for Telegram webhook in Docker.

set -e

# Install dependencies
apk add --no-cache curl jq

echo "Waiting for ngrok URL..."
PUBLIC_URL=""
for i in $(seq 1 30); do
  # Fetch public URL from ngrok API
  PUBLIC_URL=$(curl -s http://ngrok:4040/api/tunnels | jq -r '.tunnels[] | select(.proto=="https") | .public_url' | head -n1)

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

echo "✅ Public URL: $PUBLIC_URL"

if [ -z "$CODE_AGENT_TELEGRAM_BOT_TOKEN" ]; then
  echo "⚠️  CODE_AGENT_TELEGRAM_BOT_TOKEN is not set. Skipping webhook registration."
  exit 0
fi

echo "Registering Telegram webhook..."
# Include secret_token if set to secure the endpoint
WEBHOOK_DATA=$(jq -n \
  --arg url "${PUBLIC_URL}/telegram/webhook" \
  --arg secret "$CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN" \
  '{url: $url} + (if $secret != "" then {secret_token: $secret} else {} end)')

RESPONSE=$(curl -s -X POST "https://api.telegram.org/bot${CODE_AGENT_TELEGRAM_BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d "$WEBHOOK_DATA")

if echo "$RESPONSE" | grep -q '"ok":true'; then
  echo "✅ Webhook registered successfully."
else
  echo "❌ Webhook registration failed: $RESPONSE"
  exit 1
fi
