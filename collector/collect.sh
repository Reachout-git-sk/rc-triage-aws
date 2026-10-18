#!/usr/bin/env bash
# Collect Slurm jobs that ended since the last checkpoint and hand them to the Python
# triage step, which classifies the failures and sends them to the API.
#
# Safe to run from cron every few minutes:
#   * a lock directory stops two runs from overlapping (mkdir is atomic everywhere,
#     unlike flock, which some systems lack);
#   * the checkpoint moves forward only after the triage step succeeds, so a failed
#     run is simply retried by the next one; the API's upsert by job ID makes the
#     repeats harmless;
#   * each run re-reads OVERLAP_SECONDS before the checkpoint, to catch accounting
#     records that were written late;
#   * one run covers at most MAX_WINDOW_HOURS, so a long outage is caught up in pieces.
#
# Settings (environment variables). Relative paths are relative to the repo root.
#   API_URL                 the triage API, e.g. http://127.0.0.1:8080 (not needed with DRY_RUN_DIR)
#   TRIAGE_API_KEY          read by the Python step; never pass it on the command line
#   SACCT_CMD               default "sacct"; use collector/fake_sacct.sh for synthetic data
#   SACCT_DATA_DIR          where fake_sacct.sh reads from (default ./data/sacct)
#   JOB_LOG_DIR             folder with slurm-<jobid>.out files (default ./data/logs/jobs)
#   NODE_LOG_DIR            folder with <node>.log files (default ./data/logs/nodes)
#   STATE_DIR               checkpoint and lock (default ./state)
#   END_TIME                window end, UTC YYYY-MM-DDTHH:MM:SS (default: now)
#   INITIAL_LOOKBACK_HOURS  how far back the very first run starts (default 24)
#   MAX_WINDOW_HOURS        cap on one run's window (default 24)
#   OVERLAP_SECONDS         re-read this much before the checkpoint (default 300)
#   DRY_RUN_DIR             write payloads here instead of sending them
#   PYTHON                  interpreter (default python3)
set -euo pipefail

cd "$(dirname "$0")/.."

SACCT_CMD="${SACCT_CMD:-sacct}"
JOB_LOG_DIR="${JOB_LOG_DIR:-./data/logs/jobs}"
NODE_LOG_DIR="${NODE_LOG_DIR:-./data/logs/nodes}"
STATE_DIR="${STATE_DIR:-./state}"
INITIAL_LOOKBACK_HOURS="${INITIAL_LOOKBACK_HOURS:-24}"
MAX_WINDOW_HOURS="${MAX_WINDOW_HOURS:-24}"
OVERLAP_SECONDS="${OVERLAP_SECONDS:-300}"
PYTHON="${PYTHON:-python3}"

log() { printf '%s collect.sh: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
to_epoch() { date -u -d "$1" +%s; }
to_iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%S; }

mkdir -p "$STATE_DIR"
LOCK_DIR="$STATE_DIR/collect.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    log "another run (pid $old_pid) is still going; skipping this one"
    exit 0
  fi
  log "removing a stale lock left by pid ${old_pid:-unknown}"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
fi
echo "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

CHECKPOINT="$STATE_DIR/checkpoint"
now_epoch="$(to_epoch "${END_TIME:-$(date -u +%Y-%m-%dT%H:%M:%S)}")"
if [[ -s "$CHECKPOINT" ]]; then
  last="$(<"$CHECKPOINT")"
  from_epoch="$(to_epoch "$last")"
  start_epoch=$(( from_epoch - OVERLAP_SECONDS ))
else
  last=""
  from_epoch=$(( now_epoch - INITIAL_LOOKBACK_HOURS * 3600 ))
  start_epoch=$from_epoch
fi
end_epoch=$(( from_epoch + MAX_WINDOW_HOURS * 3600 ))
if (( end_epoch > now_epoch )); then
  end_epoch=$now_epoch
fi
if (( end_epoch <= from_epoch )); then
  log "already up to date (checkpoint ${last:-none}); nothing to do"
  exit 0
fi

window_start="$(to_iso "$start_epoch")"
window_end="$(to_iso "$end_epoch")"
run_id="run-$(date -u +%Y%m%dT%H%M%SZ)-$$"
sacct_file="$STATE_DIR/sacct-$run_id.txt"
log "run $run_id: jobs that ended in [$window_start, $window_end)"

"$SACCT_CMD" --allusers --parsable2 \
  --starttime "$window_start" --endtime "$window_end" \
  --state=COMPLETED,FAILED,TIMEOUT,OUT_OF_MEMORY,NODE_FAIL,CANCELLED \
  --format=JobID,JobName,User,Account,Partition,State,ExitCode,Start,End,Elapsed,Timelimit,NodeList,ReqMem,MaxRSS \
  > "$sacct_file"
log "sacct returned $(( $(wc -l < "$sacct_file") - 1 )) lines"

args=(-m triage.run --sacct "$sacct_file" --job-log-dir "$JOB_LOG_DIR" --node-log-dir "$NODE_LOG_DIR"
      --run-id "$run_id" --window-start "$window_start" --window-end "$window_end")
if [[ -n "${DRY_RUN_DIR:-}" ]]; then
  args+=(--dry-run "$DRY_RUN_DIR")
else
  args+=(--api-url "${API_URL:?set API_URL or DRY_RUN_DIR}")
fi

if "$PYTHON" "${args[@]}"; then
  # Write-then-rename: a crash mid-write can never leave a half-written checkpoint.
  printf '%s\n' "$window_end" > "$CHECKPOINT.tmp"
  mv -f "$CHECKPOINT.tmp" "$CHECKPOINT"
  rm -f "$sacct_file"
  log "run $run_id done; checkpoint is now $window_end"
else
  rc=$?
  log "run $run_id failed (exit $rc); checkpoint stays at ${last:-none}, so the next run retries."
  log "the sacct rows are kept in $sacct_file for debugging"
  exit "$rc"
fi
