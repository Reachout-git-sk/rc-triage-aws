"""Flask application factory.

Run locally:   docker compose up   (gunicorn inside the container)
gunicorn:      gunicorn -c gunicorn.conf.py "api.app:create_app()"
"""

from __future__ import annotations

import logging
import re
import time
import uuid

import psycopg
from flask import Flask, g, request
from psycopg_pool import PoolTimeout
from werkzeug.exceptions import HTTPException

from api.config import Config
from api.db import Database
from api.logging_json import configure_logging
from api.routes import bp
from api.upstream import GpuMetricsClient

log = logging.getLogger("api")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def create_app(config: Config | None = None) -> Flask:
    config = config or Config.from_env()
    configure_logging(config.log_level)

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.max_body_bytes  # bigger bodies get 413
    app.json.sort_keys = False
    app.extensions["triage.config"] = config
    app.extensions["triage.db"] = Database(
        config.database_url,
        max_size=config.db_pool_max,
        pool_timeout_s=config.db_pool_timeout_s,
        statement_timeout_ms=config.db_statement_timeout_ms,
    )
    app.extensions["triage.gpu"] = GpuMetricsClient(
        config.gpu_metrics_url,
        connect_timeout_s=config.upstream_connect_timeout_s,
        read_timeout_s=config.upstream_read_timeout_s,
        retries=config.upstream_retries,
        budget_s=config.upstream_budget_s,
    )
    app.register_blueprint(bp)

    @app.before_request
    def start_timer():
        g.t0 = time.perf_counter()
        incoming = request.headers.get("X-Request-ID", "")
        g.request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex

    @app.after_request
    def log_request(response):
        duration_ms = (time.perf_counter() - g.get("t0", time.perf_counter())) * 1000
        response.headers["X-Request-ID"] = g.get("request_id", "")
        log.info(
            "request",
            extra={
                "event": "request",
                "method": request.method,
                "path": request.path,
                "route": request.url_rule.rule if request.url_rule else None,
                "status": response.status_code,
                "duration_ms": round(duration_ms, 1),
                "request_id": g.get("request_id"),
                "version": config.app_version,
            },
        )
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        return {"error": exc.description, "request_id": g.get("request_id")}, exc.code

    @app.errorhandler(PoolTimeout)
    @app.errorhandler(psycopg.OperationalError)
    def database_unavailable(exc: Exception):
        # Includes statement_timeout cancellations (QueryCanceled).
        log.warning(
            "database unavailable", extra={"event": "db_unavailable", "error": type(exc).__name__}
        )
        body = {
            "error": "database unavailable, try again shortly",
            "request_id": g.get("request_id"),
        }
        return body, 503, {"Retry-After": "5"}

    @app.errorhandler(Exception)
    def unhandled(exc: Exception):
        log.exception("unhandled error", extra={"event": "unhandled_error"})
        return {"error": "internal error", "request_id": g.get("request_id")}, 500

    return app
