#!/usr/bin/env bash
# Runs ON the EC2 host, as root, sent by SSM Run Command (see send_deploy.sh).
#   deploy.sh <image-tag>
#
# Steps: pull the image -> run migrations -> swap the API container -> health check.
# If the new version is not healthy within ~60 s, start the previous version again
# (automatic rollback) and exit non-zero so the pipeline shows the deploy failed.
#
# Exit codes: 0 deployed, 1 failed and rolled back, 3 failed and rollback also failed.
set -euo pipefail

NEW_TAG="${1:?usage: deploy.sh <image-tag>}"
# The defaults are the EC2 host's settings; deploy/test_deploy_local.sh overrides them
# to rehearse a deploy and a rollback on a laptop.
APP_DIR="${APP_DIR:-/opt/rc-triage}"
# shellcheck source=/dev/null
source "$APP_DIR/config.env"   # written by user_data: region, repo, secret ARNs, DB host
API=rc-triage-api
UPSTREAM=rc-triage-gpu-metrics
NET=rc-triage
PORT="${PORT:-8080}"           # bound to 127.0.0.1 only; reach it through SSM port forwarding
# verify-full: encrypted AND the server must prove it is our RDS instance.
DB_TLS="${DB_TLS:-sslmode=verify-full&sslrootcert=/etc/ssl/certs/rds-global-bundle.pem}"
LOG_DRIVER="${LOG_DRIVER:-awslogs}"
HEALTH_TRIES="${HEALTH_TRIES:-30}"
CURRENT_TAG="$(cat "$APP_DIR/current_tag" 2>/dev/null || true)"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) deploy: $*"; }

secret() {
  aws secretsmanager get-secret-value --region "$AWS_REGION" --secret-id "$1" \
    --query SecretString --output text
}
json_field() { python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
urlquote() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

write_env_files() {
  local app admin app_pw api_key admin_user admin_pw
  app="$(secret "$APP_SECRET_ARN")"
  admin="$(secret "$DB_ADMIN_SECRET_ARN")"
  app_pw="$(json_field app_db_password <<<"$app")"
  api_key="$(json_field ingest_api_key <<<"$app")"
  admin_user="$(json_field username <<<"$admin")"
  admin_pw="$(json_field password <<<"$admin")"
  umask 077   # env files readable by root only
  cat > "$APP_DIR/api.env" <<EOF
DATABASE_URL=postgresql://${APP_DB_USER}:$(urlquote "$app_pw")@${DB_HOST}:${DB_PORT}/${DB_NAME}?${DB_TLS}
INGEST_API_KEY=${api_key}
GPU_METRICS_URL=http://${UPSTREAM}:9100
FAULT_INJECTION=${FAULT_INJECTION:-false}
EOF
  cat > "$APP_DIR/migrate.env" <<EOF
MIGRATE_DATABASE_URL=postgresql://$(urlquote "$admin_user"):$(urlquote "$admin_pw")@${DB_HOST}:${DB_PORT}/${DB_NAME}?${DB_TLS}
APP_DB_PASSWORD=${app_pw}
EOF
}

start_upstream() {   # the fake GPU-metrics service
  docker rm -f "$UPSTREAM" >/dev/null 2>&1 || true
  docker run -d --name "$UPSTREAM" --network "$NET" --restart unless-stopped --memory 150m \
    -e PORT=9100 -e GUNICORN_WORKERS=1 -e GUNICORN_THREADS=8 \
    "$ECR_REPO:$1" gunicorn -c gunicorn.conf.py fake_gpu_metrics.server:app >/dev/null
}

start_api() {
  local logging=(--log-driver "$LOG_DRIVER")
  if [[ "$LOG_DRIVER" == awslogs ]]; then   # container stdout -> CloudWatch Logs
    logging+=(--log-opt "awslogs-region=$AWS_REGION" --log-opt "awslogs-group=$LOG_GROUP")
  fi
  docker rm -f "$API" >/dev/null 2>&1 || true
  docker run -d --name "$API" --network "$NET" --restart unless-stopped --memory 400m \
    -p "127.0.0.1:${PORT}:8000" --env-file "$APP_DIR/api.env" -e APP_VERSION="$1" \
    "${logging[@]}" "$ECR_REPO:$1" >/dev/null
}

healthy() {   # wait up to ~60 s for /health to say ok AND report the expected version
  local _ body
  for _ in $(seq 1 "$HEALTH_TRIES"); do
    body="$(curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/health" 2>/dev/null || true)"
    if [[ -n "$body" ]] && python3 -c '
import json, sys
d = json.loads(sys.stdin.read())
sys.exit(0 if d.get("status") == "ok" and d.get("version") == sys.argv[1] else 1)' "$1" <<<"$body" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

log "deploying $NEW_TAG (running now: ${CURRENT_TAG:-nothing})"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${ECR_REPO%%/*}" >/dev/null
docker pull --quiet "$ECR_REPO:$NEW_TAG" >/dev/null
docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
write_env_files

# Migrations run BEFORE the swap and must be backward compatible ("expand" only), because
# if this deploy is rolled back, the previous version runs against the new schema.
log "running migrations"
docker run --rm --env-file "$APP_DIR/migrate.env" "$ECR_REPO:$NEW_TAG" python -m migrations.migrate

start_upstream "$NEW_TAG"
start_api "$NEW_TAG"
if healthy "$NEW_TAG"; then
  [[ -n "$CURRENT_TAG" && "$CURRENT_TAG" != "$NEW_TAG" ]] && echo "$CURRENT_TAG" > "$APP_DIR/previous_tag"
  echo "$NEW_TAG" > "$APP_DIR/current_tag"
  log "OK: $NEW_TAG is healthy and serving"
  exit 0
fi

log "FAILED: $NEW_TAG did not become healthy. Its last log lines:"
docker logs --tail 20 "$API" 2>&1 || true
if [[ -z "$CURRENT_TAG" ]]; then
  log "no previous version to roll back to"
  exit 3
fi
log "ROLLBACK: starting $CURRENT_TAG again"
start_upstream "$CURRENT_TAG"
start_api "$CURRENT_TAG"
if healthy "$CURRENT_TAG"; then
  log "rollback OK: $CURRENT_TAG is serving; the failed deploy changed nothing users can see"
  exit 1
fi
log "rollback FAILED: $CURRENT_TAG is not healthy either; needs a human"
exit 3
