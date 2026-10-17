"""PostgreSQL connection pool.

A pool keeps a few connections open so each request does not pay for a new TCP + TLS
+ auth handshake. Limits that protect the API when the database is slow or down:
  * pool timeout: wait at most N seconds for a free connection, then fail with 503;
  * statement_timeout: Postgres cancels any query that runs longer than N ms;
  * connect_timeout: give up on a database host that does not answer.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    def __init__(
        self,
        url: str,
        max_size: int = 5,
        pool_timeout_s: float = 5.0,
        statement_timeout_ms: int = 5000,
    ) -> None:
        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=max_size,
            timeout=pool_timeout_s,
            open=False,  # opened on first use, i.e. inside each gunicorn worker after fork
            name="triage-api",
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 5,
                "application_name": "rc-triage-api",
                "options": f"-c statement_timeout={int(statement_timeout_ms)}",
            },
        )
        self._lock = threading.Lock()
        self._opened = False

    @contextmanager
    def connection(self):
        """Commit when the block succeeds, roll back when it raises."""
        if not self._opened:
            with self._lock:
                if not self._opened:
                    self._pool.open(wait=False)
                    self._opened = True
        with self._pool.connection() as conn:
            yield conn

    def close(self) -> None:
        if self._opened:
            self._pool.close()
            self._opened = False
