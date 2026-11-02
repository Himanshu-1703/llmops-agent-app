#!/usr/bin/env bash
# ValidateService -- wait out the cold start, then poll /health a few times.
# /health makes one tiny real call to OpenAI and one to Jev, and its `status` is
# "healthy" only if both work. If it never reports healthy, exit non-zero so
# CodeDeploy rolls back. Runs as root.
set -euo pipefail

API=http://localhost:8000
COLD_START_WAIT=30
RETRIES=3
RETRY_GAP=30

sleep "$COLD_START_WAIT"   # give the API time to build the agent (it fetches the prompts from Langfuse)

for ((attempt = 1; attempt <= RETRIES; attempt++)); do
  response=$(curl -fsS "$API/health") || response=""
  echo "Attempt $attempt/$RETRIES: $response"

  if [ "$(echo "$response" | jq -r '.status' 2>/dev/null)" = "healthy" ]; then
    echo "Healthy"
    exit 0
  fi

  if [ "$attempt" -lt "$RETRIES" ]; then
    sleep "$RETRY_GAP"
  fi
done

echo "Health check failed after $RETRIES attempts"
exit 1
