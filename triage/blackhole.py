"""Find "black-hole" nodes: nodes that make many different users' jobs fail fast.

A healthy-looking node with a broken mount (or a dead GPU) kills each job within
seconds. Because it is always free again right away, the scheduler keeps sending it
more jobs, so one bad node can eat hundreds of jobs. One at a time, each failure looks
like a user error ("file not found", "no module named numpy"). Only the cross-job view
shows the pattern:

  * many failures on the node            (failed >= min_failed)
  * from several different users         (one user's broken script is not a node problem)
  * a failure rate far above the cluster (>= min_fail_rate and >= baseline_factor x baseline)
  * and the failures are fast            (median runtime <= max_median_elapsed_s)
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from triage.sacct import Job


@dataclass
class NodeAlert:
    node: str
    window_start: datetime
    window_end: datetime
    failed_jobs: int
    total_jobs: int
    distinct_users: int
    median_elapsed_s: int
    details: dict = field(default_factory=dict)
    alert_type: str = "blackhole"

    def as_payload(self) -> dict:
        return {
            "node": self.node,
            "alert_type": self.alert_type,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "failed_jobs": self.failed_jobs,
            "total_jobs": self.total_jobs,
            "distinct_users": self.distinct_users,
            "median_elapsed_s": self.median_elapsed_s,
            "details": self.details,
        }


def detect_blackholes(
    jobs: list[Job],
    categories: dict[str, str],
    window_start: datetime,
    window_end: datetime,
    min_failed: int = 8,
    min_users: int = 3,
    min_fail_rate: float = 0.5,
    baseline_factor: float = 5.0,
    max_median_elapsed_s: int = 120,
) -> list[NodeAlert]:
    """`categories` maps job_id -> category for the failed jobs (for the alert details)."""
    total: Counter[str] = Counter()
    failed: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        for node in job.nodes:
            total[node] += 1
            if job.failed:
                failed[node].append(job)
    baseline = (sum(1 for j in jobs if j.failed) / len(jobs)) if jobs else 0.0

    alerts: list[NodeAlert] = []
    for node, node_failed in failed.items():
        users = {j.user_name for j in node_failed}
        rate = len(node_failed) / total[node]
        median = statistics.median(j.elapsed_s or 0 for j in node_failed)
        if (
            len(node_failed) >= min_failed
            and len(users) >= min_users
            and rate >= max(min_fail_rate, baseline_factor * baseline)
            and median <= max_median_elapsed_s
        ):
            alerts.append(
                NodeAlert(
                    node=node,
                    window_start=window_start,
                    window_end=window_end,
                    failed_jobs=len(node_failed),
                    total_jobs=total[node],
                    distinct_users=len(users),
                    median_elapsed_s=int(median),
                    details={
                        "fail_rate": round(rate, 3),
                        "cluster_fail_rate": round(baseline, 3),
                        "categories": dict(
                            Counter(
                                categories.get(j.job_id, "?") for j in node_failed
                            ).most_common()
                        ),
                        "sample_job_ids": [j.job_id for j in node_failed[:10]],
                    },
                )
            )
    return sorted(alerts, key=lambda a: -a.failed_jobs)
