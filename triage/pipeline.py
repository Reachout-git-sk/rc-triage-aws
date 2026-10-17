"""Turn one window of sacct rows into failure records and node alerts (no network)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from triage.blackhole import NodeAlert, detect_blackholes
from triage.classify import classify
from triage.nodelog import NodeEventIndex
from triage.rules import node_prefilter
from triage.sacct import Job, parse_sacct
from triage.tail import DEFAULT_TAIL_BYTES, read_tail

# Hardware events up to this long before a job started still count (a GPU that fell
# off the bus 20 minutes ago is why this job sees "No CUDA GPUs are available").
NODE_LOOKBACK = timedelta(minutes=30)
NODE_SLACK = timedelta(minutes=2)


@dataclass
class Analysis:
    jobs: list[Job]
    records: list[dict]
    alerts: list[NodeAlert]

    @property
    def category_counts(self) -> dict[str, int]:
        return dict(Counter(r["category"] for r in self.records).most_common())


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def analyze(
    sacct_path: Path,
    job_log_dir: Path,
    node_log_dir: Path,
    run_id: str,
    window_start: datetime,
    window_end: datetime,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> Analysis:
    with open(sacct_path, encoding="utf-8", errors="replace") as fh:
        jobs = parse_sacct(fh)
    # A finished job always has an End time; a row without one is broken, and sending it
    # would make the API reject the whole batch.
    failed = [j for j in jobs if j.failed and j.end]
    nodes = {n for j in failed for n in j.nodes}
    index = NodeEventIndex.load(node_log_dir, nodes, node_prefilter())

    records: list[dict] = []
    for job in failed:
        log_text = read_tail(job_log_dir / f"slurm-{job.job_id}.out", tail_bytes)
        node_text = ""
        if job.start and job.end:
            node_text = index.text_between(
                job.nodes, job.start - NODE_LOOKBACK, job.end + NODE_SLACK
            )
        c = classify(job.state, log_text, node_text)
        records.append(
            {
                "job_id": job.job_id,
                "user_name": job.user_name,
                "account": job.account or None,
                "partition": job.partition or None,
                "node": job.nodes[0] if job.nodes else "unknown",
                "node_list": job.node_list or None,
                "state": job.state,
                "exit_code": job.exit_code or None,
                "start_time": _iso(job.start),
                "end_time": _iso(job.end),
                "elapsed_s": job.elapsed_s,
                "req_mem_mb": job.req_mem_mb,
                "max_rss_mb": job.max_rss_mb,
                "category": c.category,
                "owner": c.owner,
                "rule": c.rule,
                "evidence": c.evidence
                if log_text is not None or c.evidence
                else "job log not found",
                "hint": c.hint,
                "node_suspect": False,
            }
        )

    categories = {r["job_id"]: r["category"] for r in records}
    alerts = detect_blackholes(jobs, categories, window_start, window_end)
    flagged = {a.node for a in alerts}
    by_id = {j.job_id: j for j in failed}
    for r in records:
        r["node_suspect"] = any(n in flagged for n in by_id[r["job_id"]].nodes)
    return Analysis(jobs=jobs, records=records, alerts=alerts)
