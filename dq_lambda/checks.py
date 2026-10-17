"""Daily data-quality checks on the triage database (read-only).

Each check returns PASS, WARN, FAIL or SKIP with the number it measured, so a person
reading the report sees *how far* off something is, not just that it is off.

  freshness       when did the collector last report? (a quiet cluster still reports)
  volume          failures in the last 24 h vs. the average of the 7 days before
  duplicates      the same job stored under two different IDs
  missing_fields  recently written rows with empty required fields
  unknown_share   share of failures no rule could classify (rule-table coverage)
  reconciliation  every job a collector run said it found must be in the database

The functions take any DB-API connection with the "format" paramstyle (%s), so the
same code runs with pg8000 in Lambda and in the tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
SEVERITY = {PASS: 0, SKIP: 0, WARN: 1, FAIL: 2}


@dataclass
class Thresholds:
    freshness_max_hours: float = 26.0  # the collector reports at least daily in this demo
    volume_low_ratio: float = 0.33
    volume_high_ratio: float = 3.0
    volume_min_history_days: int = 3
    unknown_warn: float = 0.05
    unknown_fail: float = 0.15


@dataclass
class CheckResult:
    check: str
    status: str
    value: float | int | None
    threshold: str
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


def _one(conn, query: str, params: tuple = ()) -> tuple:
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        return cur.fetchone()
    finally:
        cur.close()


def _all(conn, query: str, params: tuple = ()) -> list[tuple]:
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        return list(cur.fetchall())
    finally:
        cur.close()


def check_freshness(conn, now: datetime, t: Thresholds) -> CheckResult:
    (last,) = _one(conn, "SELECT max(received_at) FROM collector_runs")
    limit = f"<= {t.freshness_max_hours:g} h since the last collector run"
    if last is None:
        return CheckResult("freshness", FAIL, None, limit, "no collector run has ever reported")
    age_h = round((now - last).total_seconds() / 3600, 2)
    status = PASS if age_h <= t.freshness_max_hours else FAIL
    return CheckResult(
        "freshness", status, age_h, limit, f"last run received at {last.isoformat()}"
    )


def check_volume(conn, now: datetime, t: Thresholds) -> CheckResult:
    rows = _all(
        conn,
        "SELECT floor(extract(epoch FROM (%s::timestamptz - end_time)) / 86400)::int AS days_ago, count(*)"
        " FROM failures WHERE end_time >= %s::timestamptz - interval '8 days' AND end_time < %s::timestamptz"
        " GROUP BY 1",
        (now, now, now),
    )
    per_day = dict(rows)
    (oldest,) = _one(conn, "SELECT min(end_time) FROM failures")
    limit = (
        f"last 24 h within {t.volume_low_ratio:g}x-{t.volume_high_ratio:g}x of the 7-day average"
    )
    history_days = min(7, int((now - oldest).total_seconds() // 86400)) if oldest else 0
    last_24h = per_day.get(0, 0)
    if history_days < t.volume_min_history_days:
        return CheckResult(
            "volume",
            SKIP,
            last_24h,
            limit,
            f"only {history_days} full day(s) of history; need {t.volume_min_history_days}",
        )
    avg = sum(per_day.get(d, 0) for d in range(1, history_days + 1)) / history_days
    if avg == 0:
        return CheckResult(
            "volume", PASS if last_24h == 0 else WARN, last_24h, limit, "no failures in history"
        )
    ratio = round(last_24h / avg, 2)
    if last_24h == 0 and avg >= 10:
        status = FAIL  # usually means the pipeline broke, not that the cluster became perfect
    elif ratio < t.volume_low_ratio or ratio > t.volume_high_ratio:
        status = WARN  # big swings are often real events (an outage, a new user): look, don't page
    else:
        status = PASS
    return CheckResult(
        "volume", status, ratio, limit, f"{last_24h} in the last 24 h vs. {avg:.1f}/day"
    )


def check_duplicates(conn, now: datetime, t: Thresholds) -> CheckResult:
    # job_id is the primary key, so exact duplicates cannot exist. The real risk is the
    # same job arriving under two IDs (e.g. an array task as "123_4" and as its raw ID).
    (groups,) = _one(
        conn,
        "SELECT count(*) FROM (SELECT 1 FROM failures WHERE end_time >= %s::timestamptz - interval '7 days'"
        " GROUP BY user_name, node, start_time, end_time HAVING count(*) > 1) d",
        (now,),
    )
    status = PASS if groups == 0 else FAIL
    return CheckResult(
        "duplicates",
        status,
        groups,
        "0 jobs stored under two IDs (last 7 days)",
        "same user, node, start and end under different job_ids",
    )


def check_missing_fields(conn, now: datetime, t: Thresholds) -> CheckResult:
    required_missing, no_start, optional_missing, total = _one(
        conn,
        "SELECT"
        " count(*) FILTER (WHERE coalesce(user_name, '') = '' OR coalesce(node, '') = ''"
        "   OR coalesce(state, '') = '' OR coalesce(category, '') = ''"
        "   OR coalesce(owner, '') = '' OR coalesce(rule, '') = ''),"
        " count(*) FILTER (WHERE start_time IS NULL AND state <> 'NODE_FAIL'),"
        " count(*) FILTER (WHERE coalesce(account, '') = '' OR coalesce(partition, '') = ''),"
        " count(*)"
        " FROM failures WHERE updated_at >= %s::timestamptz - interval '24 hours'",
        (now,),
    )
    detail = (
        f"{required_missing} rows miss a required field, {no_start} miss start_time, "
        f"{optional_missing} miss account/partition, of {total} written in 24 h"
    )
    if required_missing or no_start:
        status = FAIL
    elif total and optional_missing / total > 0.01:
        status = WARN
    else:
        status = PASS
    return CheckResult("missing_fields", status, required_missing + no_start, "0 rows", detail)


def check_unknown_share(conn, now: datetime, t: Thresholds) -> CheckResult:
    unknown, total = _one(
        conn,
        "SELECT count(*) FILTER (WHERE owner = 'human_review'), count(*)"
        " FROM failures WHERE end_time >= %s::timestamptz - interval '24 hours' AND end_time < %s::timestamptz",
        (now, now),
    )
    limit = f"warn > {t.unknown_warn:.0%}, fail > {t.unknown_fail:.0%}"
    if total == 0:
        return CheckResult("unknown_share", SKIP, None, limit, "no failures in the last 24 h")
    share = round(unknown / total, 4)
    status = FAIL if share > t.unknown_fail else WARN if share > t.unknown_warn else PASS
    return CheckResult(
        "unknown_share", status, share, limit, f"{unknown} of {total} failures had no matching rule"
    )


def check_reconciliation(conn, now: datetime, t: Thresholds) -> CheckResult:
    rows = _all(
        conn,
        "SELECT r.run_id, r.send_errors,"
        " (SELECT count(*) FROM unnest(r.job_ids) AS j(job_id)"
        "   WHERE NOT EXISTS (SELECT 1 FROM failures f WHERE f.job_id = j.job_id)) AS missing,"
        " cardinality(r.job_ids) AS expected"
        " FROM collector_runs r WHERE r.received_at >= %s::timestamptz - interval '24 hours'",
        (now,),
    )
    limit = "every job a run reported is in the failures table"
    if not rows:
        return CheckResult(
            "reconciliation", SKIP, None, limit, "no collector runs in the last 24 h"
        )
    missing = sum(r[2] for r in rows)
    expected = sum(r[3] for r in rows)
    bad_runs = [r[0] for r in rows if r[2]]
    errored = [r[0] for r in rows if r[1]]
    if missing:
        status = FAIL
    elif errored:
        status = WARN  # runs had send errors, but later runs filled the gaps
    else:
        status = PASS
    detail = f"{missing} of {expected} reported jobs missing across {len(rows)} run(s)"
    if bad_runs:
        detail += f"; runs with gaps: {', '.join(bad_runs[:5])}"
    if errored:
        detail += f"; runs with send errors: {len(errored)}"
    return CheckResult("reconciliation", status, missing, limit, detail)


CHECKS = (
    check_freshness,
    check_volume,
    check_duplicates,
    check_missing_fields,
    check_unknown_share,
    check_reconciliation,
)


def run_all(conn, now: datetime, thresholds: Thresholds | None = None) -> dict:
    t = thresholds or Thresholds()
    results = [check(conn, now, t) for check in CHECKS]
    worst = max(results, key=lambda r: SEVERITY[r.status])
    overall = worst.status if SEVERITY[worst.status] else PASS
    return {
        "checked_at": now.isoformat(),
        "status": overall,
        "failed": sum(r.status == FAIL for r in results),
        "warned": sum(r.status == WARN for r in results),
        "checks": [r.as_dict() for r in results],
    }
