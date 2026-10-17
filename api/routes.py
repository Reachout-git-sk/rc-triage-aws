"""HTTP endpoints of the triage API."""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, date, datetime, time, timedelta
from functools import wraps

import psycopg
from flask import Blueprint, current_app, request
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout
from werkzeug.exceptions import BadRequest, NotFound

from api.validation import JOB_ID, NAME, Invalid, parse_ts, validate_ingest

log = logging.getLogger("api")
bp = Blueprint("api", __name__)

FAILURE_COLUMNS = (
    "job_id",
    "user_name",
    "account",
    "partition",
    "node",
    "node_list",
    "state",
    "exit_code",
    "start_time",
    "end_time",
    "elapsed_s",
    "req_mem_mb",
    "max_rss_mb",
    "category",
    "owner",
    "rule",
    "evidence",
    "hint",
    "node_suspect",
    "run_id",
    "first_seen_at",
    "updated_at",
)
SELECT_FAILURES = sql.SQL("SELECT {} FROM failures").format(
    sql.SQL(", ").join(sql.Identifier(c) for c in FAILURE_COLUMNS)
)
# query-string name -> column
FAILURE_FILTERS = {
    "category": "category",
    "owner": "owner",
    "node": "node",
    "user": "user_name",
    "state": "state",
    "rule": "rule",
}

# Idempotent upsert, one statement for the whole batch.
#  * jsonb_to_recordset turns the JSON array into rows with typed columns.
#  * ON CONFLICT (job_id) updates the existing row instead of adding a duplicate.
#  * The WHERE ... IS DISTINCT FROM skips rows whose content did not change, so sending
#    the same batch twice writes nothing the second time.
#  * node_suspect is "sticky" (old OR new): a later, smaller window that cannot see the
#    black hole must not clear a flag an earlier window set.
#  * (xmax = 0) is true for a freshly inserted row (a Postgres detail): it lets us
#    report inserted vs updated counts.
UPSERT_FAILURES = """
INSERT INTO failures AS f (
    job_id, user_name, account, partition, node, node_list, state, exit_code, start_time,
    end_time, elapsed_s, req_mem_mb, max_rss_mb, category, owner, rule, evidence, hint,
    node_suspect, run_id)
SELECT job_id, user_name, account, partition, node, node_list, state, exit_code, start_time,
       end_time, elapsed_s, req_mem_mb, max_rss_mb, category, owner, rule, evidence, hint,
       node_suspect, run_id
FROM jsonb_to_recordset(%(rows)s) AS x(
    job_id text, user_name text, account text, partition text, node text, node_list text,
    state text, exit_code text, start_time timestamptz, end_time timestamptz,
    elapsed_s integer, req_mem_mb integer, max_rss_mb integer, category text, owner text,
    rule text, evidence text, hint text, node_suspect boolean, run_id text)
ON CONFLICT (job_id) DO UPDATE SET
    user_name = EXCLUDED.user_name, account = EXCLUDED.account,
    partition = EXCLUDED.partition, node = EXCLUDED.node, node_list = EXCLUDED.node_list,
    state = EXCLUDED.state, exit_code = EXCLUDED.exit_code,
    start_time = EXCLUDED.start_time, end_time = EXCLUDED.end_time,
    elapsed_s = EXCLUDED.elapsed_s, req_mem_mb = EXCLUDED.req_mem_mb,
    max_rss_mb = EXCLUDED.max_rss_mb, category = EXCLUDED.category, owner = EXCLUDED.owner,
    rule = EXCLUDED.rule, evidence = EXCLUDED.evidence, hint = EXCLUDED.hint,
    node_suspect = f.node_suspect OR EXCLUDED.node_suspect,
    run_id = EXCLUDED.run_id, updated_at = now()
WHERE (f.user_name, f.account, f.partition, f.node, f.node_list, f.state, f.exit_code,
       f.start_time, f.end_time, f.elapsed_s, f.req_mem_mb, f.max_rss_mb, f.category,
       f.owner, f.rule, f.evidence, f.hint, f.node_suspect)
      IS DISTINCT FROM
      (EXCLUDED.user_name, EXCLUDED.account, EXCLUDED.partition, EXCLUDED.node,
       EXCLUDED.node_list, EXCLUDED.state, EXCLUDED.exit_code, EXCLUDED.start_time,
       EXCLUDED.end_time, EXCLUDED.elapsed_s, EXCLUDED.req_mem_mb, EXCLUDED.max_rss_mb,
       EXCLUDED.category, EXCLUDED.owner, EXCLUDED.rule, EXCLUDED.evidence, EXCLUDED.hint,
       f.node_suspect OR EXCLUDED.node_suspect)
RETURNING (xmax = 0) AS inserted
"""

UPSERT_ALERTS = """
INSERT INTO node_alerts AS a (
    node, alert_type, window_start, window_end, failed_jobs, total_jobs, distinct_users,
    median_elapsed_s, details, run_id)
SELECT node, alert_type, window_start, window_end, failed_jobs, total_jobs, distinct_users,
       median_elapsed_s, COALESCE(details, '{}'::jsonb), run_id
FROM jsonb_to_recordset(%(rows)s) AS x(
    node text, alert_type text, window_start timestamptz, window_end timestamptz,
    failed_jobs integer, total_jobs integer, distinct_users integer, median_elapsed_s integer,
    details jsonb, run_id text)
ON CONFLICT (node, alert_type, window_start) DO UPDATE SET
    window_end = EXCLUDED.window_end, failed_jobs = EXCLUDED.failed_jobs,
    total_jobs = EXCLUDED.total_jobs, distinct_users = EXCLUDED.distinct_users,
    median_elapsed_s = EXCLUDED.median_elapsed_s, details = EXCLUDED.details,
    run_id = EXCLUDED.run_id, updated_at = now()
WHERE (a.window_end, a.failed_jobs, a.total_jobs, a.distinct_users, a.median_elapsed_s,
       a.details)
      IS DISTINCT FROM
      (EXCLUDED.window_end, EXCLUDED.failed_jobs, EXCLUDED.total_jobs,
       EXCLUDED.distinct_users, EXCLUDED.median_elapsed_s, EXCLUDED.details)
"""

UPSERT_RUN = """
INSERT INTO collector_runs (
    run_id, window_start, window_end, sacct_rows, failures_found, failures_sent, send_errors,
    job_ids, category_counts, started_at, finished_at)
VALUES (
    %(run_id)s, %(window_start)s, %(window_end)s, %(sacct_rows)s, %(failures_found)s,
    %(failures_sent)s, %(send_errors)s, %(job_ids)s::text[], %(category_counts)s,
    %(started_at)s, %(finished_at)s)
ON CONFLICT (run_id) DO UPDATE SET
    window_start = EXCLUDED.window_start, window_end = EXCLUDED.window_end,
    sacct_rows = EXCLUDED.sacct_rows, failures_found = EXCLUDED.failures_found,
    failures_sent = EXCLUDED.failures_sent, send_errors = EXCLUDED.send_errors,
    job_ids = EXCLUDED.job_ids, category_counts = EXCLUDED.category_counts,
    started_at = EXCLUDED.started_at, finished_at = EXCLUDED.finished_at,
    received_at = now()
"""


def cfg():
    return current_app.extensions["triage.config"]


def db():
    return current_app.extensions["triage.db"]


def jsonable(row: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


def int_arg(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadRequest(f"{name} must be a whole number") from None
    if value < minimum or (maximum is not None and value > maximum):
        raise BadRequest(f"{name} must be between {minimum} and {maximum}")
    return value


def ts_arg(name: str) -> datetime:
    try:
        return parse_ts(request.args[name])
    except Invalid as exc:
        raise BadRequest(f"{name} {exc}") from None


def require_api_key(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        expected = cfg().ingest_api_key
        if not expected:
            return {"error": "ingest is disabled: no API key is configured"}, 503
        given = request.headers.get("X-API-Key", "")
        # compare_digest takes the same time whether the first or the last character
        # differs, so the key cannot be guessed one character at a time from timings.
        if not hmac.compare_digest(given.encode(), expected.encode()):
            return {"error": "missing or invalid API key"}, 401
        return view(*args, **kwargs)

    return wrapper


@bp.get("/health")
def health():
    version = cfg().app_version
    try:
        with db().connection() as conn:
            conn.execute("SELECT 1")
    except (PoolTimeout, psycopg.OperationalError) as exc:
        log.warning(
            "health check: database unavailable",
            extra={"event": "health_db_down", "error": type(exc).__name__},
        )
        return {"status": "degraded", "db": "unavailable", "version": version}, 503
    return {"status": "ok", "db": "ok", "version": version}


@bp.get("/api/failures")
def list_failures():
    c = cfg()
    warnings: list[str] = []
    limit = int_arg("limit", c.default_page_size, minimum=1)
    if limit > c.max_page_size:
        warnings.append(
            f"limit {limit} is above the maximum, so it was capped at {c.max_page_size}"
        )
        limit = c.max_page_size
    offset = int_arg("offset", 0, minimum=0, maximum=100_000)

    conditions: list[sql.Composable] = []
    params: dict = {"limit": limit + 1, "offset": offset}
    for arg, column in FAILURE_FILTERS.items():
        value = request.args.get(arg)
        if value:
            conditions.append(
                sql.SQL("{} = {}").format(sql.Identifier(column), sql.Placeholder(arg))
            )
            params[arg] = value[:100]
    if request.args.get("since"):
        params["since"] = ts_arg("since")
        conditions.append(sql.SQL("end_time >= %(since)s"))
    if request.args.get("until"):
        params["until"] = ts_arg("until")
        conditions.append(sql.SQL("end_time < %(until)s"))
    suspect = request.args.get("node_suspect")
    if suspect:
        if suspect not in ("true", "false"):
            raise BadRequest("node_suspect must be true or false")
        params["node_suspect"] = suspect == "true"
        conditions.append(sql.SQL("node_suspect = %(node_suspect)s"))

    where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions) if conditions else sql.SQL("")
    query = sql.SQL(
        "{} {} ORDER BY end_time DESC, job_id DESC LIMIT %(limit)s OFFSET %(offset)s"
    ).format(SELECT_FAILURES, where)
    with db().connection() as conn:
        rows = conn.execute(query, params).fetchall()
    # We asked for one extra row: if it came back, there is another page.
    has_more = len(rows) > limit
    items = [jsonable(r) for r in rows[:limit]]
    return {
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if has_more else None,
        "warnings": warnings,
    }


@bp.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    if not JOB_ID.match(job_id):
        raise BadRequest("job_id must look like 5001234 or 5001234_7")
    with db().connection() as conn:
        row = conn.execute(SELECT_FAILURES + sql.SQL(" WHERE job_id = %s"), (job_id,)).fetchone()
    if row is None:
        raise NotFound(f"no failure recorded for job {job_id}")
    return jsonable(row)


@bp.get("/api/nodes/<node>/health")
def node_health(node: str):
    if not NAME.match(node):
        raise BadRequest("node must be 1-64 letters, digits, '.', '_' or '-'")
    hours = int_arg("hours", 24, minimum=1, maximum=720)
    params = {"node": node, "hours": hours}
    with db().connection() as conn:
        totals = conn.execute(
            """
            SELECT count(*) AS total, count(DISTINCT user_name) AS distinct_users,
                   max(end_time) AS last_failure_at
            FROM failures
            WHERE node = %(node)s AND end_time >= now() - make_interval(hours => %(hours)s::int)
            """,
            params,
        ).fetchone()
        by_category = conn.execute(
            """
            SELECT category, owner, count(*) AS n
            FROM failures
            WHERE node = %(node)s AND end_time >= now() - make_interval(hours => %(hours)s::int)
            GROUP BY category, owner
            ORDER BY n DESC, category
            """,
            params,
        ).fetchall()
        alerts = conn.execute(
            """
            SELECT alert_type, window_start, window_end, failed_jobs, total_jobs,
                   distinct_users, median_elapsed_s, details
            FROM node_alerts
            WHERE node = %(node)s AND window_end >= now() - make_interval(hours => %(hours)s::int)
            ORDER BY window_start DESC
            LIMIT 20
            """,
            params,
        ).fetchall()
    # The database connection is back in the pool BEFORE we call the slow upstream,
    # so a hanging GPU service cannot use up all database connections.
    metrics = current_app.extensions["triage.gpu"].node_metrics(node)
    warnings = [metrics.warning] if metrics.warning else []
    sysadmin_failures = sum(r["n"] for r in by_category if r["owner"] == "sysadmin")
    if alerts or sysadmin_failures:
        verdict = "suspect"
    elif totals["total"]:
        verdict = "failures_but_no_node_signal"
    else:
        verdict = "ok"
    return {
        "node": node,
        "window_hours": hours,
        "verdict": verdict,
        "failures": {
            **jsonable(totals),
            "by_category": [dict(r) for r in by_category],
        },
        "alerts": [jsonable(a) for a in alerts],
        "gpu_metrics": metrics.data,
        "partial": bool(warnings),
        "warnings": warnings,
    }


@bp.get("/api/summary/daily")
def daily_summary():
    raw = request.args.get("date")
    try:
        day = date.fromisoformat(raw) if raw else datetime.now(UTC).date()
    except ValueError:
        raise BadRequest("date must be YYYY-MM-DD") from None
    start = datetime.combine(day, time.min, UTC)
    params = {"start": start, "end": start + timedelta(days=1)}
    with db().connection() as conn:
        groups = conn.execute(
            """
            SELECT category, owner, count(*) AS n
            FROM failures
            WHERE end_time >= %(start)s AND end_time < %(end)s
            GROUP BY category, owner
            ORDER BY n DESC
            """,
            params,
        ).fetchall()
        top_nodes = conn.execute(
            """
            SELECT node, count(*) AS failures, count(DISTINCT user_name) AS users
            FROM failures
            WHERE end_time >= %(start)s AND end_time < %(end)s
            GROUP BY node
            ORDER BY failures DESC, node
            LIMIT 5
            """,
            params,
        ).fetchall()
        extra = conn.execute(
            """
            SELECT count(DISTINCT user_name) AS users,
                   count(*) FILTER (WHERE node_suspect) AS node_suspect_failures
            FROM failures
            WHERE end_time >= %(start)s AND end_time < %(end)s
            """,
            params,
        ).fetchone()
        alerts = conn.execute(
            """
            SELECT DISTINCT ON (node) node, failed_jobs, distinct_users, median_elapsed_s
            FROM node_alerts
            WHERE window_end > %(start)s AND window_start < %(end)s
            ORDER BY node, failed_jobs DESC
            """,
            params,
        ).fetchall()
    by_category: dict[str, int] = {}
    by_owner: dict[str, int] = {}
    for g in groups:
        by_category[g["category"]] = by_category.get(g["category"], 0) + g["n"]
        by_owner[g["owner"]] = by_owner.get(g["owner"], 0) + g["n"]
    total = sum(by_category.values())
    return {
        "date": day.isoformat(),
        "total_failures": total,
        "users_affected": extra["users"],
        "by_category": by_category,
        "by_owner": by_owner,
        "unknown_share": round(by_owner.get("human_review", 0) / total, 4) if total else 0.0,
        "node_suspect_failures": extra["node_suspect_failures"],
        "top_nodes": [dict(r) for r in top_nodes],
        "blackhole_alerts": [dict(a) for a in alerts],
    }


@bp.post("/api/ingest")
@require_api_key
def ingest():
    body = request.get_json(silent=True)
    if body is None:
        raise BadRequest("body must be JSON with Content-Type: application/json")
    clean, errors = validate_ingest(body, cfg().max_ingest_rows)
    if errors:
        return {"error": "invalid payload; nothing was saved", "problems": errors}, 400
    run_id = clean["run_id"]

    # The same job twice in one batch would make Postgres refuse the upsert
    # ("cannot affect row a second time"), so keep the last copy of each.
    rows: dict[str, dict] = {}
    for row in clean["failures"]:
        rows[row["job_id"]] = {**row, "run_id": run_id}
    alerts: dict[tuple, dict] = {}
    for a in clean["node_alerts"]:
        alerts[(a["node"], a["alert_type"], a["window_start"])] = {**a, "run_id": run_id}
    summary = clean["run_summary"]

    inserted = updated = alerts_changed = 0
    with db().connection() as conn:  # one transaction: all of it is saved, or none of it
        if rows:
            flags = conn.execute(UPSERT_FAILURES, {"rows": Jsonb(list(rows.values()))}).fetchall()
            inserted = sum(1 for f in flags if f["inserted"])
            updated = len(flags) - inserted
        if alerts:
            alerts_changed = conn.execute(
                UPSERT_ALERTS, {"rows": Jsonb(list(alerts.values()))}
            ).rowcount
        if summary:
            conn.execute(
                UPSERT_RUN,
                {
                    **summary,
                    "run_id": run_id,
                    "category_counts": Jsonb(summary["category_counts"] or {}),
                },
            )
    result = {
        "run_id": run_id,
        "received": len(clean["failures"]),
        "duplicates_in_batch": len(clean["failures"]) - len(rows),
        "inserted": inserted,
        "updated": updated,
        "unchanged": len(rows) - inserted - updated,
        "alerts_changed": alerts_changed,
        "run_summary_saved": summary is not None,
    }
    log.info("ingest", extra={"event": "ingest", **result})
    return result


@bp.post("/api/_fault")
@require_api_key
def fault():
    """Fail on purpose so the 5xx alarm can be tested. Off unless FAULT_INJECTION=true."""
    if not cfg().fault_injection:
        raise NotFound()
    raise RuntimeError("fault injected on purpose (FAULT_INJECTION=true)")
