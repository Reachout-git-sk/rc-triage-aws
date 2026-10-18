#!/usr/bin/env bash
# Run deploy.sh on the EC2 host through SSM Run Command and wait for the result.
# Used by the CD workflow, and from a laptop for the very first deploy.
#   send_deploy.sh <instance-id> <image-tag>
# Optional: FAULT_INJECTION=true (alarm drills), AWS_REGION, PYTHON.
#
# No SSH and no open port: the SSM agent on the host fetches the command over its own
# outbound HTTPS connection, runs it as root, and uploads the output.
set -euo pipefail

INSTANCE_ID="${1:?usage: send_deploy.sh <instance-id> <image-tag>}"
TAG="${2:?usage: send_deploy.sh <instance-id> <image-tag>}"
REGION="${AWS_REGION:-us-east-1}"
PYTHON="${PYTHON:-python3}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# Send the script itself (base64, so no quoting problems), then run it.
script_b64="$(base64 -w0 < "$HERE/deploy.sh")"
params="$("$PYTHON" -c '
import json, sys
b64, tag, fault = sys.argv[1:4]
print(json.dumps({
    "commands": [
        "set -e",
        f"echo {b64} | base64 -d > /opt/rc-triage/deploy.sh",
        "chmod 700 /opt/rc-triage/deploy.sh",
        f"FAULT_INJECTION={fault} /opt/rc-triage/deploy.sh {tag}",
    ],
    "executionTimeout": ["600"],
}))' "$script_b64" "$TAG" "${FAULT_INJECTION:-false}")"

command_id="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript --comment "deploy $TAG" \
  --parameters "$params" --query Command.CommandId --output text)"
echo "SSM command $command_id: deploying $TAG to $INSTANCE_ID"

status=Pending
for _ in $(seq 1 120); do
  sleep 5
  status="$(aws ssm get-command-invocation --region "$REGION" --command-id "$command_id" \
    --instance-id "$INSTANCE_ID" --query Status --output text 2>/dev/null || echo Pending)"
  case "$status" in
    Pending|InProgress|Delayed) continue ;;
    *) break ;;
  esac
done

echo "----- output from the host -----"
aws ssm get-command-invocation --region "$REGION" --command-id "$command_id" \
  --instance-id "$INSTANCE_ID" --query '[StandardOutputContent, StandardErrorContent]' --output text
echo "--------------------------------"
echo "status: $status"
[[ "$status" == "Success" ]]
