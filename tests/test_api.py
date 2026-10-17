from datetime import UTC, datetime, timedelta

import psycopg
import pytest
import requests
import responses

from tests.conftest import API_KEY, GPU_URL, auth, failure

pytestmark = pytest.mark.db


def ingest(client, failures=None, run_id="run-1", **extra):
    body = {"run_id": run_id, "failures": failures or [], **extra}
    return client.post("/api/ingest", json=body, headers=auth())


def count_rows(admin_url, table="failures") -> int:
    with psycopg.connect(admin_url) as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608


def test_health_reports_version(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json() == {"status": "ok", "db": "ok", "version": "test-sha"}
    assert r.headers["X-Request-ID"]


def test_ingest_requires_the_api_key(client):
    body = {"run_id": "r", "failures": [failure()]}
    assert client.post("/api/ingest", json=body).status_code == 401
    assert client.post("/api/ingest", json=body, headers={"X-API-Key": "wrong"}).status_code == 401


def test_ingest_is_refused_when_no_key_is_configured(make_app):
    c = make_app(ingest_api_key="").test_client()
    assert c.post("/api/ingest", json={"run_id": "r"}, headers=auth()).status_code == 503


def test_ingest_then_read_back(client):
    r = ingest(client, [failure("5000001"), failure("5000002", node="cpu-002")])
    assert r.status_code == 200
    assert r.get_json() == {
        "run_id": "run-1",
        "received": 2,
        "duplicates_in_batch": 0,
        "inserted": 2,
        "updated": 0,
        "unchanged": 0,
        "alerts_changed": 0,
        "run_summary_saved": False,
    }
    job = client.get("/api/jobs/5000002").get_json()
    assert job["node"] == "cpu-002" and job["category"] == "out_of_memory"
    assert job["end_time"].endswith("+00:00")


def test_sending_the_same_batch_twice_changes_nothing(client, clean_db):
    batch = [failure(str(5000000 + i)) for i in range(50)]
    first = ingest(client, batch, run_id="run-a").get_json()
    with psycopg.connect(clean_db) as conn:
        before = conn.execute(
            "SELECT job_id, updated_at, run_id FROM failures ORDER BY 1"
        ).fetchall()
    second = ingest(client, batch, run_id="run-b").get_json()
    with psycopg.connect(clean_db) as conn:
        after = conn.execute(
            "SELECT job_id, updated_at, run_id FROM failures ORDER BY 1"
        ).fetchall()
    assert first["inserted"] == 50
    assert (second["inserted"], second["updated"], second["unchanged"]) == (0, 0, 50)
    assert before == after  # not even updated_at moved
    assert count_rows(clean_db) == 50


def test_changed_classification_updates_the_row(client):
    ingest(
        client,
        [failure("5000001", category="unknown", owner="human_review", rule="no_rule_matched")],
    )
    r = ingest(client, [failure("5000001")], run_id="run-2").get_json()
    assert (r["inserted"], r["updated"]) == (0, 1)
    job = client.get("/api/jobs/5000001").get_json()
    assert job["category"] == "out_of_memory" and job["run_id"] == "run-2"


def test_duplicate_job_in_one_batch_keeps_the_last_copy(client, clean_db):
    r = ingest(client, [failure("5000001", node="cpu-001"), failure("5000001", node="cpu-009")])
    assert r.status_code == 200
    assert r.get_json()["duplicates_in_batch"] == 1
    assert client.get("/api/jobs/5000001").get_json()["node"] == "cpu-009"
    assert count_rows(clean_db) == 1


def test_node_suspect_flag_is_sticky(client):
    ingest(client, [failure("5000001", node_suspect=True)])
    r = ingest(client, [failure("5000001", node_suspect=False)], run_id="run-2").get_json()
    assert r["unchanged"] == 1
    assert client.get("/api/jobs/5000001").get_json()["node_suspect"] is True


def test_one_bad_row_rejects_the_whole_batch(client, clean_db):
    bad = failure("5000002", owner="nobody", end_time="yesterday")
    r = ingest(client, [failure("5000001"), bad])
    assert r.status_code == 400
    problems = r.get_json()["problems"]
    assert any("failures[1].owner" in p for p in problems)
    assert any("failures[1].end_time" in p for p in problems)
    assert count_rows(clean_db) == 0


def test_too_many_rows_is_rejected(make_app):
    c = make_app(max_ingest_rows=3).test_client()
    r = ingest(c, [failure(str(5000000 + i)) for i in range(4)])
    assert r.status_code == 400


def test_run_summary_and_alerts_are_saved(client, clean_db):
    now = datetime.now(UTC)
    alert = {
        "node": "cpu-087",
        "alert_type": "blackhole",
        "window_start": (now - timedelta(hours=24)).isoformat(),
        "window_end": now.isoformat(),
        "failed_jobs": 41,
        "total_jobs": 51,
        "distinct_users": 35,
        "median_elapsed_s": 22,
        "details": {"fail_rate": 0.8},
    }
    summary = {
        "window_start": alert["window_start"],
        "window_end": alert["window_end"],
        "sacct_rows": 2764,
        "failures_found": 1,
        "failures_sent": 1,
        "send_errors": 0,
        "job_ids": ["5000001"],
        "category_counts": {"out_of_memory": 1},
    }
    ingest(client, [failure("5000001")])
    r = ingest(client, [], node_alerts=[alert], run_summary=summary).get_json()
    assert r["alerts_changed"] == 1 and r["run_summary_saved"]
    again = ingest(client, [], node_alerts=[alert], run_summary=summary).get_json()
    assert again["alerts_changed"] == 0
    assert count_rows(clean_db, "collector_runs") == 1


def test_list_failures_filters_and_pages(client):
    ingest(
        client,
        [failure(str(5000000 + i), node=f"cpu-00{i % 3}") for i in range(7)]
        + [failure("5000100", category="timeout", rule="timeout_state", state="TIMEOUT")],
    )
    page = client.get("/api/failures?node=cpu-001&limit=2").get_json()
    assert page["count"] == 2 and page["next_offset"] == 2
    assert all(i["node"] == "cpu-001" for i in page["items"])
    last = client.get("/api/failures?node=cpu-001&limit=2&offset=2").get_json()
    assert last["count"] == 0 or last["next_offset"] is None
    timeouts = client.get("/api/failures?category=timeout").get_json()
    assert [i["job_id"] for i in timeouts["items"]] == ["5000100"]


def test_list_limit_is_capped_with_a_warning(client):
    r = client.get("/api/failures?limit=5000").get_json()
    assert r["limit"] == 500
    assert "capped at 500" in r["warnings"][0]


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=abc", "offset=-1", "since=yesterday", "node_suspect=maybe"]
)
def test_bad_query_parameters_get_400(client, query):
    r = client.get(f"/api/failures?{query}")
    assert r.status_code == 400 and "error" in r.get_json()


def test_job_not_found_and_bad_id(client):
    assert client.get("/api/jobs/5999999").status_code == 404
    assert client.get("/api/jobs/abc").status_code == 400


def test_daily_summary(client):
    end = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    ingest(
        client,
        [
            failure("5000001", end=end),
            failure("5000002", end=end, node="cpu-087", node_suspect=True),
            failure(
                "5000003", end=end, category="unknown", owner="human_review", rule="no_rule_matched"
            ),
            failure("5000004", end=end + timedelta(days=1)),
        ],
    )
    s = client.get("/api/summary/daily?date=2026-10-16").get_json()
    assert s["total_failures"] == 3
    assert s["by_owner"] == {"user": 2, "human_review": 1}
    assert s["unknown_share"] == round(1 / 3, 4)
    assert s["node_suspect_failures"] == 1
    assert client.get("/api/summary/daily?date=16-10-2026").status_code == 400


def test_node_health_with_upstream_ok(client):
    ingest(
        client,
        [
            failure(
                "5000001",
                node="gpu-013",
                category="gpu_hardware",
                owner="sysadmin",
                rule="gpu_xid_79",
            )
        ],
    )
    with responses.RequestsMock() as mock:
        mock.get(f"{GPU_URL}/metrics/gpu-013", json={"node": "gpu-013", "xid_events_24h": [79]})
        body = client.get("/api/nodes/gpu-013/health").get_json()
    assert body["partial"] is False and body["warnings"] == []
    assert body["gpu_metrics"]["xid_events_24h"] == [79]
    assert body["verdict"] == "suspect"
    assert body["failures"]["total"] == 1


def test_node_health_is_partial_when_upstream_times_out(client):
    with responses.RequestsMock() as mock:
        mock.get(f"{GPU_URL}/metrics/gpu-001", body=requests.exceptions.ReadTimeout())
        r = client.get("/api/nodes/gpu-001/health")
    body = r.get_json()
    assert r.status_code == 200
    assert body["partial"] is True and body["gpu_metrics"] is None
    assert body["warnings"] == ["gpu_metrics unavailable: timed out after 2 attempt(s)"]
    assert body["failures"]["total"] == 0  # the database part still answered


def test_node_health_for_cpu_node_has_no_warning(client):
    with responses.RequestsMock() as mock:
        mock.get(f"{GPU_URL}/metrics/cpu-001", status=404)
        body = client.get("/api/nodes/cpu-001/health").get_json()
    assert body["partial"] is False and body["gpu_metrics"] is None and body["verdict"] == "ok"


def test_fault_endpoint_is_off_by_default(client):
    assert client.post("/api/_fault", headers=auth()).status_code == 404


def test_fault_endpoint_returns_json_500_when_enabled(make_app, caplog):
    c = make_app(fault_injection=True).test_client()
    r = c.post("/api/_fault", headers=auth())
    assert r.status_code == 500
    assert r.get_json()["error"] == "internal error" and r.get_json()["request_id"]
    logged = [rec for rec in caplog.records if getattr(rec, "event", None) == "request"]
    assert logged[-1].status == 500 and logged[-1].route == "/api/_fault"


def test_database_down_gives_503_not_500(make_app):
    c = make_app(
        database_url="postgresql://nobody:x@127.0.0.1:1/none", db_pool_timeout_s=0.5
    ).test_client()
    r = c.get("/api/failures")
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"
    assert c.get("/health").status_code == 503


def test_request_body_too_large(make_app):
    c = make_app(max_body_bytes=1000).test_client()
    r = c.post(
        "/api/ingest", data="x" * 2000, headers={**auth(), "Content-Type": "application/json"}
    )
    assert r.status_code == 413


def test_api_key_is_never_logged(make_app, caplog):
    c = make_app().test_client()
    ingest(c, [failure()])
    assert API_KEY not in caplog.text
