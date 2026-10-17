"""Triage one collector window and send the results to the API.

Called by collector/collect.sh. Exit codes:
  0  everything was sent (the collector may move its checkpoint forward)
  1  some batches could not be sent (keep the checkpoint; the next run re-sends them)
  2  bad input or a crash

The API key comes from the TRIAGE_API_KEY environment variable, never from the
command line (command lines are visible to every user on the machine via `ps`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from triage.client import IngestClient, IngestError
from triage.pipeline import analyze

log = logging.getLogger("triage.run")


def _utc(value: str) -> datetime:
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--sacct", type=Path, required=True)
    p.add_argument("--job-log-dir", type=Path, required=True)
    p.add_argument("--node-log-dir", type=Path, required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--window-start", type=_utc, required=True)
    p.add_argument("--window-end", type=_utc, required=True)
    p.add_argument("--api-url", default=os.environ.get("API_URL", ""))
    p.add_argument("--dry-run", type=Path, help="write payloads to this folder instead of sending")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument(
        "--max-attempts", type=int, default=int(os.environ.get("TRIAGE_MAX_ATTEMPTS", "5"))
    )
    return p.parse_args(argv)


class DryRunSink:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.count = 0
        folder.mkdir(parents=True, exist_ok=True)

    def post(self, payload: dict) -> dict:
        self.count += 1
        path = self.folder / f"{payload['run_id']}-{self.count:04d}.json"
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        return {"dry_run": str(path)}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args(argv)
    started = datetime.now(UTC)
    t0 = time.perf_counter()
    try:
        result = analyze(
            args.sacct,
            args.job_log_dir,
            args.node_log_dir,
            args.run_id,
            args.window_start,
            args.window_end,
        )
    except Exception:
        log.exception("triage failed before anything was sent")
        return 2
    analysis_s = time.perf_counter() - t0

    if args.dry_run:
        sink = DryRunSink(args.dry_run)
    else:
        api_key = os.environ.get("TRIAGE_API_KEY", "")
        if not args.api_url or not api_key:
            log.error("set --api-url (or API_URL) and TRIAGE_API_KEY, or use --dry-run")
            return 2
        sink = IngestClient(args.api_url, api_key, max_attempts=args.max_attempts)

    sent, unsent, totals = 0, 0, {"inserted": 0, "updated": 0, "unchanged": 0}
    records = result.records
    for i in range(0, len(records), args.batch_size):
        batch = records[i : i + args.batch_size]
        try:
            resp = sink.post({"run_id": args.run_id, "failures": batch})
        except IngestError as exc:
            unsent += len(batch)
            log.error(
                "batch %d (%d records) not sent: %s", i // args.batch_size + 1, len(batch), exc
            )
            if not exc.retryable:
                break  # a 4xx will fail the same way for every batch
            continue
        sent += len(batch)
        for key in totals:
            totals[key] += int(resp.get(key, 0))

    # The run summary goes last. The data-quality check later compares its job_ids
    # with what the database really holds ("reconciliation").
    summary = {
        "window_start": args.window_start.isoformat(),
        "window_end": args.window_end.isoformat(),
        "sacct_rows": len(result.jobs),
        "failures_found": len(records),
        "failures_sent": sent,
        "send_errors": len(records) - sent,
        "job_ids": [r["job_id"] for r in records],
        "category_counts": result.category_counts,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
    }
    summary_saved = True
    try:
        sink.post(
            {
                "run_id": args.run_id,
                "failures": [],
                "node_alerts": [a.as_payload() for a in result.alerts],
                "run_summary": summary,
            }
        )
    except IngestError as exc:
        summary_saved = False
        log.error("run summary / node alerts not sent: %s", exc)

    report = {
        "run_id": args.run_id,
        "jobs": len(result.jobs),
        "failures": len(records),
        "sent": sent,
        "unsent": len(records) - sent,
        **totals,
        "node_alerts": [a.node for a in result.alerts],
        "categories": result.category_counts,
        "summary_saved": summary_saved,
        "analysis_seconds": round(analysis_s, 2),
        "total_seconds": round(time.perf_counter() - t0, 2),
    }
    print(json.dumps(report))
    return 0 if sent == len(records) and summary_saved else 1


if __name__ == "__main__":
    sys.exit(main())
