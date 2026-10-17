"""Apply SQL migrations and make sure the database roles exist.

Run as the database admin (the RDS master user in AWS, `postgres` locally):
    python -m migrations.migrate

Environment:
    MIGRATE_DATABASE_URL  admin connection string (falls back to DATABASE_URL)
    APP_DB_USER           role the API logs in as (default triage_app)
    APP_DB_PASSWORD       its password; if empty, the app role is left alone
    DQ_DB_USER            read-only role for the data-quality Lambda (default dq_reader)
    DQ_DB_PASSWORD        optional password for local use; in AWS the role logs in with
                          short-lived IAM tokens instead (the rds_iam grant below)

Rules for writing migrations (so a rollback of the *code* is always safe):
    * only add things (tables, nullable columns, indexes) in the same release as the code
      that uses them ("expand");
    * remove or rename things only in a later release, once no running version needs
      them ("contract");
    * never edit a migration that already ran anywhere; add a new file instead.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql

log = logging.getLogger("migrations")

SQL_DIR = Path(__file__).with_name("sql")
LOCK_KEY = 727_274_001  # any constant: pg_advisory_lock makes concurrent migrators wait
APP_TABLES = ("failures", "node_alerts", "collector_runs")


def _ensure_login_role(conn: psycopg.Connection, name: str, password: str) -> None:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
    verb = "ALTER" if exists else "CREATE"
    query = sql.SQL(verb + " ROLE {} WITH LOGIN").format(sql.Identifier(name))
    if password:
        # Literal() quotes the value safely. DDL cannot take bind parameters.
        query = sql.SQL("{} PASSWORD {}").format(query, sql.Literal(password))
    conn.execute(query)


def ensure_roles(
    conn: psycopg.Connection, app_user: str, app_password: str, dq_user: str, dq_password: str
) -> None:
    db = sql.Identifier(conn.info.dbname)
    tables = sql.SQL(", ").join(sql.Identifier(t) for t in APP_TABLES)
    if app_password:
        _ensure_login_role(conn, app_user, app_password)
        role = sql.Identifier(app_user)
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, role))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        # No DELETE, no DDL: the API only reads and upserts.
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON {} TO {}").format(tables, role))

    _ensure_login_role(conn, dq_user, dq_password)
    role = sql.Identifier(dq_user)
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, role))
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(tables, role))
    # rds_iam exists only on Amazon RDS: it lets the role log in with an IAM auth token.
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam'").fetchone():
        conn.execute(sql.SQL("GRANT rds_iam TO {}").format(role))


def apply(
    url: str,
    app_user: str = "triage_app",
    app_password: str = "",
    dq_user: str = "dq_reader",
    dq_password: str = "",
) -> list[str]:
    """Apply pending migrations in order; return the versions applied this time."""
    applied_now: list[str] = []
    with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for path in sorted(SQL_DIR.glob("*.sql")):
                if path.stem in done:
                    continue
                with conn.transaction():  # a failed migration leaves no half-applied schema
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,)
                    )
                applied_now.append(path.stem)
                log.info("applied migration %s", path.stem)
            ensure_roles(conn, app_user, app_password, dq_user, dq_password)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return applied_now


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    url = os.environ.get("MIGRATE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        log.error("set MIGRATE_DATABASE_URL (admin connection string)")
        return 2
    applied = apply(
        url,
        app_user=os.environ.get("APP_DB_USER", "triage_app"),
        app_password=os.environ.get("APP_DB_PASSWORD", ""),
        dq_user=os.environ.get("DQ_DB_USER", "dq_reader"),
        dq_password=os.environ.get("DQ_DB_PASSWORD", ""),
    )
    log.info("migrations up to date (%d applied now: %s)", len(applied), ", ".join(applied) or "-")
    return 0


if __name__ == "__main__":
    sys.exit(main())
