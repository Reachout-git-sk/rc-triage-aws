"""Lambda entry point: run the data-quality checks once and report.

How it reaches the private RDS database without internet or a NAT gateway:
  * The Lambda runs inside the VPC's private subnets, next to RDS.
  * It logs in as the read-only `dq_reader` role with an **IAM auth token**. boto3
    makes that token by signing locally with the Lambda's own role credentials: there
    is no network call, so no Secrets Manager VPC endpoint is needed.
  * It reports by printing JSON lines. Lambda ships its output to CloudWatch Logs
    itself (outside the VPC), and one line uses the Embedded Metric Format, which
    CloudWatch turns into the metric `RcTriage/DataQuality FailedChecks`. An alarm on
    that metric sends the email, so no SNS endpoint is needed either.

A failed check is *data* news, not a Lambda error: the function still succeeds.
The Lambda only errors (and trips the Lambda-errors alarm) when it cannot run the checks.

Local run (plain password, no TLS):
    DQ_DATABASE_URL=postgresql://postgres:...@127.0.0.1:5436/triage python -m dq_lambda.handler
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import pg8000.dbapi

from dq_lambda.checks import run_all

CA_BUNDLE = Path(__file__).with_name("rds-global-bundle.pem")
METRIC_NAMESPACE = "RcTriage/DataQuality"


def connect():
    local_url = os.environ.get("DQ_DATABASE_URL")
    if local_url:
        u = urlparse(local_url)
        return pg8000.dbapi.connect(
            user=unquote(u.username or ""),
            password=unquote(u.password or ""),
            host=u.hostname,
            port=u.port or 5432,
            database=u.path.lstrip("/"),
            timeout=10,
        )
    import boto3  # in the Lambda runtime already; not needed for local runs

    host = os.environ["DB_HOST"]
    port = int(os.environ.get("DB_PORT", "5432"))
    user = os.environ.get("DB_USER", "dq_reader")
    region = os.environ["AWS_REGION"]
    token = boto3.client("rds", region_name=region).generate_db_auth_token(
        DBHostname=host, Port=port, DBUsername=user, Region=region
    )
    # Verify the server's certificate against Amazon's RDS CA bundle (packaged in the zip).
    context = ssl.create_default_context(cafile=str(CA_BUNDLE))
    return pg8000.dbapi.connect(
        user=user,
        password=token,
        host=host,
        port=port,
        database=os.environ.get("DB_NAME", "triage"),
        ssl_context=context,
        timeout=10,
    )


def emf_line(report: dict) -> str:
    """A CloudWatch Embedded Metric Format record: a log line that is also a metric."""
    return json.dumps(
        {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": METRIC_NAMESPACE,
                        "Dimensions": [["Service"]],
                        "Metrics": [
                            {"Name": "FailedChecks", "Unit": "Count"},
                            {"Name": "WarnedChecks", "Unit": "Count"},
                        ],
                    }
                ],
            },
            "Service": "rc-triage",
            "FailedChecks": report["failed"],
            "WarnedChecks": report["warned"],
        }
    )


def handler(event=None, context=None) -> dict:
    now = datetime.now(UTC)
    conn = connect()
    try:
        report = run_all(conn, now)
    finally:
        conn.close()
    for check in report["checks"]:
        print(json.dumps({"event": "dq_check", **check}))
    print(
        json.dumps(
            {
                "event": "dq_report",
                "status": report["status"],
                "failed": report["failed"],
                "warned": report["warned"],
            }
        )
    )
    print(emf_line(report))
    return report


if __name__ == "__main__":
    result = handler()
    sys.exit(0 if result["status"] != "FAIL" else 1)
