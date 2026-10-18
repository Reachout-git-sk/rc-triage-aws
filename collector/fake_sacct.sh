#!/usr/bin/env bash
# A stand-in for Slurm's `sacct`, reading the synthetic files from synth/generate.py.
# It understands only the flags collect.sh uses:
#   -S/--starttime, -E/--endtime   window on the job's End time: start <= End < end
#   -s/--state                     comma-separated states ("CANCELLED" matches "CANCELLED by 123")
#   -P/--parsable2, -a/--allusers, -n/--noheader, -o/--format
#     (--format is accepted, but the synthetic files always have the same columns)
# Real sacct has many more options and subtler window rules; see the README.
set -euo pipefail

DATA_DIR="${SACCT_DATA_DIR:-./data/sacct}"
start="" end="" states="" header=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    -S|--starttime) start="$2"; shift 2 ;;
    --starttime=*)  start="${1#*=}"; shift ;;
    -E|--endtime)   end="$2"; shift 2 ;;
    --endtime=*)    end="${1#*=}"; shift ;;
    -s|--state)     states="$2"; shift 2 ;;
    --state=*)      states="${1#*=}"; shift ;;
    -o|--format)    shift 2 ;;
    --format=*)     shift ;;
    -n|--noheader)  header=0; shift ;;
    -P|--parsable2|-a|--allusers) shift ;;
    *) echo "fake_sacct: unsupported option: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$start" || -z "$end" ]]; then
  echo "fake_sacct: --starttime and --endtime are required" >&2
  exit 2
fi

shopt -s nullglob
files=("$DATA_DIR"/*.txt)
if (( ${#files[@]} == 0 )); then
  echo "fake_sacct: no sacct files in $DATA_DIR (run: python -m synth.generate)" >&2
  exit 1
fi

# Columns: $1 JobID, $6 State, $9 End. Step lines (.batch/.extern) follow their job's decision.
awk -F'|' -v start="$start" -v end="$end" -v states="$states" -v header="$header" '
  BEGIN { n = split(states, s, ","); for (i = 1; i <= n; i++) want[s[i]] = 1 }
  FNR == 1 { if (header && !printed) { print; printed = 1 }; next }
  {
    base = $1; sub(/\..*/, "", base)
    if ($1 == base) {
      split($6, st, " ")
      keep[base] = ($9 >= start && $9 < end && (n == 0 || (st[1] in want)))
    }
    if (keep[base]) print
  }
' "${files[@]}"
