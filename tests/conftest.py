"""Shared fixtures.

Database tests need a throwaway PostgreSQL database given as an admin URL:
    TEST_DATABASE_URL=postgresql://postgres:<password>@127.0.0.1:5436/triage_test
Without it they are skipped. Tests use their own roles (triage_app_test, dq_reader_test)
because roles are shared by every database on a server.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from api.app import create_app
from api.config import Config

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
APP_USER = "triage_app_test"
APP_PASSWORD = "test-only-app-password"
DQ_USER = "dq_reader_test"
API_KEY = "test-api-key"
GPU_URL = "http://gpu-metrics.test"


@pytest.fixture(scope="session")
def admin_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("set TEST_DATABASE_URL to run database tests")
    from migrations.migrate import apply

    apply(TEST_DATABASE_URL, app_user=APP_USER, app_password=APP_PASSWORD, dq_user=DQ_USER)
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def app_url(admin_url: str) -> str:
    return make_conninfo(admin_url, user=APP_USER, password=APP_PASSWORD)


@pytest.fixture
def clean_db(admin_url: str) -> str:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("TRUNCATE failures, node_alerts, collector_runs")
    return admin_url


@pytest.fixture
def make_app(clean_db: str, app_url: str):
    apps = []

    def factory(**overrides):
        settings = {
            "database_url": app_url,
            "ingest_api_key": API_KEY,
            "gpu_metrics_url": GPU_URL,
            "upstream_retries": 1,
            "upstream_budget_s": 1.0,
            "app_version": "test-sha",
        }
        settings.update(overrides)
        app = create_app(Config(**settings))
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.extensions["triage.db"].close()


@pytest.fixture
def client(make_app):
    return make_app().test_client()


# Fixed once per test session, so two calls to failure() describe the same job identically.
DEFAULT_END = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)


def failure(job_id: str = "5000001", **overrides) -> dict:
    """A valid failure record, as the collector would send it."""
    end = overrides.pop("end", DEFAULT_END)
    record = {
        "job_id": job_id,
        "user_name": "user007",
        "account": "lab08",
        "partition": "batch",
        "node": "cpu-001",
        "node_list": "cpu-001",
        "state": "OUT_OF_MEMORY",
        "exit_code": "0:125",
        "start_time": (end - timedelta(minutes=30)).isoformat(),
        "end_time": end.isoformat(),
        "elapsed_s": 1800,
        "req_mem_mb": 4096,
        "max_rss_mb": 4095,
        "category": "out_of_memory",
        "owner": "user",
        "rule": "oom_state",
        "evidence": "sacct State=OUT_OF_MEMORY",
        "hint": "Request more memory.",
        "node_suspect": False,
    }
    record.update(overrides)
    return record


def auth() -> dict:
    return {"X-API-Key": API_KEY}
