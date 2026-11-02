#!/usr/bin/env bash
# ApplicationStart -- writes .env from Secrets Manager, logs in to ECR, pulls the
# images and starts the stack. docker-compose.yml is already in APP_DIR (the
# appspec `files:` block copied it from this bundle). Runs as root.
set -euo pipefail

REGION=ap-south-1
ACCOUNT_ID=891377050051
SECRET_ID=hiring-agent-api-keys
APP_DIR=/opt/hiring-agent
APP_USER=ubuntu
ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

cd "$APP_DIR"

# 4. Write .env from Secrets Manager
apt-get install -y jq
aws secretsmanager get-secret-value \
  --secret-id "$SECRET_ID" \
  --region "$REGION" \
  --query SecretString --output text \
  | jq -r 'to_entries[] | "\(.key)=\(.value)"' > "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"
cut -d= -f1 "$APP_DIR/.env"   # sanity check: key names, no values

# 6. Log in to ECR, pull the images, start the stack
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"
docker compose pull
docker compose up -d
docker compose ps
