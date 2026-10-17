"""Check POST /api/ingest payloads before they reach the database.

A batch is all-or-nothing: if any row is invalid the whole request gets a 400 with a
list of problems, and nothing is written. The collector treats 400 as "do not retry".
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

JOB_ID = re.compile(r"^\d{1,12}(_\d{1,7})?$")
RUN_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CATEGORY = re.compile(r"^[a-z0-9_]{1,64}$")
OWNERS = frozenset({"user", "hpc_support", "sysadmin", "human_review"})
ALERT_TYPES = frozenset({"blackhole"})
MAX_ERRORS = 20

# field -> (kind, required). Unknown fields are ignored, so an older API still accepts
# a newer collector that sends an extra field during a rolling upgrade.
FAILURE_FIELDS: dict[str, tuple[str, bool]] = {
    "job_id": ("job_id", True),
    "user_name": ("name", True),
    "account": ("name", False),
    "partition": ("name", False),
    "node": ("name", True),
    "node_list": ("text200", False),
    "state": ("name", True),
    "exit_code": ("text200", False),
    "start_time": ("ts", False),
    "end_time": ("ts", True),
    "elapsed_s": ("int", False),
    "req_mem_mb": ("int", False),
    "max_rss_mb": ("int", False),
    "category": ("category", True),
    "owner": ("owner", True),
    "rule": ("category", True),
    "evidence": ("text1000", False),
    "hint": ("text1000", False),
    "node_suspect": ("bool", False),
}
ALERT_FIELDS: dict[str, tuple[str, bool]] = {
    "node": ("name", True),
    "alert_type": ("alert_type", True),
    "window_start": ("ts", True),
    "window_end": ("ts", True),
    "failed_jobs": ("int", True),
    "total_jobs": ("int", True),
    "distinct_users": ("int", True),
    "median_elapsed_s": ("int", True),
    "details": ("dict", False),
}
SUMMARY_FIELDS: dict[str, tuple[str, bool]] = {
    "window_start": ("ts", True),
    "window_end": ("ts", True),
    "sacct_rows": ("int", True),
    "failures_found": ("int", True),
    "failures_sent": ("int", True),
    "send_errors": ("int", True),
    "job_ids": ("job_ids", True),
    "category_counts": ("dict", False),
    "started_at": ("ts", False),
    "finished_at": ("ts", False),
}


class Invalid(ValueError):
    pass


def parse_ts(value: object) -> datetime:
    if not isinstance(value, str):
        raise Invalid("must be an ISO 8601 string")
    try:
        t = datetime.fromisoformat(value)
    except ValueError as exc:
        raise Invalid("must be an ISO 8601 timestamp") from exc
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _check(kind: str, value: object) -> object:
    text_kinds = ("name", "category", "job_id", "owner", "alert_type", "text200", "text1000")
    if kind in text_kinds and not isinstance(value, str):
        raise Invalid("must be a string")
    if kind == "name" and not NAME.match(value):
        raise Invalid("must be 1-64 letters, digits, '.', '_' or '-'")
    if kind == "category" and not CATEGORY.match(value):
        raise Invalid("must be 1-64 lowercase letters, digits or '_'")
    if kind == "job_id" and not JOB_ID.match(value):
        raise Invalid("must look like 5001234 or 5001234_7")
    if kind == "owner" and value not in OWNERS:
        raise Invalid(f"must be one of {sorted(OWNERS)}")
    if kind == "alert_type" and value not in ALERT_TYPES:
        raise Invalid(f"must be one of {sorted(ALERT_TYPES)}")
    if kind == "text200":
        return value[:200]
    if kind == "text1000":
        return value[:1000]
    if kind == "ts":
        return parse_ts(value).isoformat()
    # bool is a subclass of int in Python, so exclude it explicitly.
    is_small_int = isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**31
    if kind == "int" and not is_small_int:
        raise Invalid("must be a whole number between 0 and 2^31")
    if kind == "bool" and not isinstance(value, bool):
        raise Invalid("must be true or false")
    if kind == "dict":
        if not isinstance(value, dict):
            raise Invalid("must be an object")
        if len(json.dumps(value)) > 10_000:
            raise Invalid("is larger than 10 KB")
    if kind == "job_ids":
        if not isinstance(value, list) or len(value) > 200_000:
            raise Invalid("must be a list of at most 200,000 job IDs")
        if not all(isinstance(v, str) and JOB_ID.match(v) for v in value):
            raise Invalid("contains something that is not a job ID")
    return value


def clean_row(
    row: object, fields: dict[str, tuple[str, bool]], where: str, errors: list[str]
) -> dict:
    if not isinstance(row, dict):
        errors.append(f"{where}: must be an object")
        return {}
    out: dict = {}
    for name, (kind, required) in fields.items():
        value = row.get(name)
        if value is None:
            if required:
                errors.append(f"{where}.{name}: is required")
            out[name] = None
            continue
        try:
            out[name] = _check(kind, value)
        except Invalid as exc:
            errors.append(f"{where}.{name}: {exc}")
    return out


def validate_ingest(body: object, max_rows: int) -> tuple[dict, list[str]]:
    """Return (clean payload, errors). The payload is only usable when errors is empty."""
    errors: list[str] = []
    if not isinstance(body, dict):
        return {}, ["body must be a JSON object"]
    run_id = body.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.match(run_id):
        errors.append("run_id: must be 1-100 letters, digits or ._:-")
    failures = body.get("failures", [])
    alerts = body.get("node_alerts", [])
    summary = body.get("run_summary")
    if not isinstance(failures, list) or len(failures) > max_rows:
        errors.append(f"failures: must be a list of at most {max_rows} rows")
        failures = []
    if not isinstance(alerts, list) or len(alerts) > 200:
        errors.append("node_alerts: must be a list of at most 200 rows")
        alerts = []
    clean = {
        "run_id": run_id,
        "failures": [
            clean_row(r, FAILURE_FIELDS, f"failures[{i}]", errors) for i, r in enumerate(failures)
        ],
        "node_alerts": [
            clean_row(r, ALERT_FIELDS, f"node_alerts[{i}]", errors) for i, r in enumerate(alerts)
        ],
        "run_summary": None
        if summary is None
        else clean_row(summary, SUMMARY_FIELDS, "run_summary", errors),
    }
    for row in clean["failures"]:
        if row.get("node_suspect") is None:
            row["node_suspect"] = False
    return clean, errors[:MAX_ERRORS]
