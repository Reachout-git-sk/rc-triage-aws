#!/usr/bin/env bash
# Rehearse deploy.sh on a laptop, including an automatic rollback, before trying it on AWS.
#
# Needs: Docker, the local stack running (docker compose up), and .env.
# Fakes only what exists on EC2: `aws` (returns the local .env values as "secrets") and
# `docker pull/login` (the images are already local). Everything else is real: the
# migrations, the container swap, the health check and the rollback.
#
#   bash deploy/test_deploy_local.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
set -a
# shellcheck source=/dev/null
source .env
set +a

WORK="$REPO/state/deploy-test"
rm -rf "$WORK"
mkdir -p "$WORK/bin" "$WORK/app"
REAL_DOCKER="$(command -v docker)"
PY="${PYTHON:-$REPO/.venv/Scripts/python.exe}"
[[ -x "$PY" ]] || PY="$(command -v python3)"
export REAL_DOCKER PY

cat > "$WORK/bin/aws" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *get-secret-value*app-secret*)   printf '{"app_db_password":"%s","ingest_api_key":"%s"}\n' "$APP_DB_PASSWORD" "$INGEST_API_KEY" ;;
  *get-secret-value*admin-secret*) printf '{"username":"postgres","password":"%s"}\n' "$POSTGRES_PASSWORD" ;;
  *"ecr get-login-password"*)      echo not-a-real-token ;;
  *) echo "fake aws: unexpected call: $*" >&2; exit 1 ;;
esac
EOF
cat > "$WORK/bin/docker" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  login) cat >/dev/null; exit 0 ;;
  pull)  exit 0 ;;
  *)     exec "$REAL_DOCKER" "$@" ;;
esac
EOF
cat > "$WORK/bin/python3" <<'EOF'
#!/usr/bin/env bash
exec "$PY" "$@"
EOF
chmod +x "$WORK/bin/"*

cat > "$WORK/app/config.env" <<EOF
AWS_REGION=us-east-1
ECR_REPO=rc-triage-deploytest
APP_SECRET_ARN=app-secret
DB_ADMIN_SECRET_ARN=admin-secret
DB_HOST=host.docker.internal
DB_PORT=5436
DB_NAME=triage
APP_DB_USER=triage_app
LOG_GROUP=unused
EOF

docker image inspect rc-triage-api:local >/dev/null  # built by docker compose
docker tag rc-triage-api:local rc-triage-deploytest:v1
docker tag rc-triage-api:local rc-triage-deploytest:v2
# A bad release: same image, but the app crashes on startup.
printf 'FROM rc-triage-api:local\nCMD ["python", "-c", "raise SystemExit(\\"simulated bad release: crash on startup\\")"]\n' \
  | docker build -q -t rc-triage-deploytest:broken - >/dev/null

run_deploy() {
  PATH="$WORK/bin:$PATH" APP_DIR="$WORK/app" PORT=8081 DB_TLS="sslmode=disable" \
    LOG_DRIVER=json-file HEALTH_TRIES="${HEALTH_TRIES:-30}" bash deploy/deploy.sh "$1"
}
version() { curl -fsS --max-time 3 http://127.0.0.1:8081/health | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["version"])'; }
check() { if [[ "$1" == "$2" ]]; then echo "PASS: $3"; else echo "FAIL: $3 (got '$1', want '$2')"; exit 1; fi; }
cleanup() {
  "$REAL_DOCKER" rm -f rc-triage-api rc-triage-gpu-metrics >/dev/null 2>&1 || true
  "$REAL_DOCKER" network rm rc-triage >/dev/null 2>&1 || true
  "$REAL_DOCKER" rmi rc-triage-deploytest:v1 rc-triage-deploytest:v2 rc-triage-deploytest:broken >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== 1. first deploy (v1)"
run_deploy v1
check "$(version)" v1 "v1 is serving"

echo "== 2. normal upgrade (v1 -> v2)"
run_deploy v2
check "$(version)" v2 "v2 is serving"
check "$(cat "$WORK/app/previous_tag")" v1 "previous_tag remembers v1"

echo "== 3. bad release (v2 -> broken) must roll back to v2"
set +e
HEALTH_TRIES=8 run_deploy broken
rc=$?
set -e
check "$rc" 1 "deploy.sh exits 1 (failed, rolled back)"
check "$(version)" v2 "v2 is serving again after the rollback"
check "$(cat "$WORK/app/current_tag")" v2 "current_tag still says v2"
echo "all deploy rehearsal checks passed"
