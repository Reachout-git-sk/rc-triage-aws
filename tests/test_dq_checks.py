import json
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlparse

import pg8000.dbapi
import psycopg
import pytest

from dq_lambda import handler as dq_handler
from dq_lambda.checks import run_all

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def pg(clean_db):
    """A pg8000 connection, the same driver the Lambda uses."""
    u = urlparse(clean_db)
    conn = pg8000.dbapi.connect(
        user=unquote(u.username),
        password=unquote(u.password or ""),
        host=u.hostname,
        port=u.port or 5432,
        database=u.path.lstrip("/"),
    )
    yield conn
    conn.close()


def seed(
    admin_url,
    *,
    days=6,
    per_day=20,
    unknown_every=0,
    last_run_age_h=1.0,
    run_job_ids=None,
    send_errors=0,
    extra_sql=(),
):
    """Failures spread over `days` days before NOW, plus one collector run."""
    rows, n = [], 0
    for d in range(days):
        for i in range(per_day):
            n += 1
            end = NOW - timedelta(days=d, hours=1, minutes=i)
            unknown = unknown_every and n % unknown_every == 0
            rows.append(
                (
                    str(5000000 + n),
                    f"user{i:03d}",
                    "lab01",
                    "batch",
                    f"cpu-{i:03d}",
                    "FAILED",
                    end - timedelta(minutes=5),
                    end,
                    "unknown" if unknown else "missing_file",
                    "human_review" if unknown else "user",
                )
            )
    with psycopg.connect(admin_url) as conn:
        conn.cursor().executemany(
            "INSERT INTO failures (job_id, user_name, account, partition, node, state, start_time,"
            " end_time, category, owner, rule, run_id, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'r', 'seed', %s)",
            [(*r, NOW - timedelta(hours=1)) for r in rows],
        )
        ids = run_job_ids if run_job_ids is not None else [r[0] for r in rows[:per_day]]
        conn.execute(
            "INSERT INTO collector_runs (run_id, window_start, window_end, sacct_rows, failures_found,"
            " failures_sent, send_errors, job_ids, received_at)"
            " VALUES ('run-1', %s, %s, 500, %s, %s, %s, %s, %s)",
            (
                NOW - timedelta(days=1),
                NOW,
                len(ids),
                len(ids) - send_errors,
                send_errors,
                ids,
                NOW - timedelta(hours=last_run_age_h),
            ),
        )
        for stmt in extra_sql:
            conn.execute(stmt)


def by_name(report):
    return {c["check"]: c for c in report["checks"]}


def test_healthy_data_passes(clean_db, pg):
    seed(clean_db)
    report = run_all(pg, NOW)
    assert report["status"] == "PASS", report
    assert {c["status"] for c in report["checks"]} == {"PASS"}


def test_stale_collector_fails_freshness(clean_db, pg):
    seed(clean_db, last_run_age_h=30)
    assert by_name(run_all(pg, NOW))["freshness"]["status"] == "FAIL"


def test_no_runs_at_all_fails_freshness(clean_db, pg):
    assert by_name(run_all(pg, NOW))["freshness"]["status"] == "FAIL"


def test_volume_needs_history(clean_db, pg):
    seed(clean_db, days=2)
    assert by_name(run_all(pg, NOW))["volume"]["status"] == "SKIP"


def test_volume_spike_warns(clean_db, pg):
    seed(clean_db)
    with psycopg.connect(clean_db) as conn:
        conn.execute(
            "INSERT INTO failures (job_id, user_name, node, state, start_time, end_time,"
            " category, owner, rule, run_id)"
            " SELECT (6000000 + g)::text, 'user001', 'cpu-001', 'FAILED', %s,"
            " %s + g * interval '1 second', 'missing_file', 'user', 'r', 'x'"
            " FROM generate_series(1, 100) g",
            (NOW - timedelta(hours=3), NOW - timedelta(hours=2)),
        )
    v = by_name(run_all(pg, NOW))["volume"]
    assert v["status"] == "WARN" and v["value"] > 3


def test_same_job_under_two_ids_fails_duplicates(clean_db, pg):
    seed(
        clean_db,
        extra_sql=[
            "INSERT INTO failures (job_id, user_name, account, partition, node, state, start_time, end_time,"
            " category, owner, rule, run_id)"
            " SELECT '7000001', user_name, account, partition, node, state, start_time, end_time,"
            " category, owner, rule, run_id FROM failures WHERE job_id = '5000001'",
        ],
    )
    assert by_name(run_all(pg, NOW))["duplicates"]["status"] == "FAIL"


def test_empty_required_field_fails(clean_db, pg):
    seed(clean_db, extra_sql=["UPDATE failures SET user_name = '' WHERE job_id = '5000003'"])
    assert by_name(run_all(pg, NOW))["missing_fields"]["status"] == "FAIL"


def test_high_unknown_share_fails(clean_db, pg):
    seed(clean_db, unknown_every=4)  # 25 % unknown
    u = by_name(run_all(pg, NOW))["unknown_share"]
    assert u["status"] == "FAIL" and u["value"] == 0.25


def test_reported_job_missing_from_db_fails_reconciliation(clean_db, pg):
    seed(clean_db, run_job_ids=["5000001", "5000002", "9999999"])
    r = by_name(run_all(pg, NOW))["reconciliation"]
    assert r["status"] == "FAIL" and r["value"] == 1


def test_send_errors_without_gaps_only_warn(clean_db, pg):
    seed(clean_db, send_errors=2)
    assert by_name(run_all(pg, NOW))["reconciliation"]["status"] == "WARN"


def test_handler_prints_report_and_emf_metric(clean_db, monkeypatch, capsys):
    seed(clean_db)
    monkeypatch.setenv("DQ_DATABASE_URL", clean_db)
    report = dq_handler.handler({}, None)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sum(1 for line in lines if line.get("event") == "dq_check") == 6
    emf = lines[-1]
    assert emf["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "RcTriage/DataQuality"
    assert emf["FailedChecks"] == report["failed"]
