"""Settings, read once from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    database_url: str
    ingest_api_key: str = ""  # empty -> POST /api/ingest is refused (fail closed)
    gpu_metrics_url: str = ""  # empty -> node health says the upstream is not configured
    upstream_connect_timeout_s: float = 0.5
    upstream_read_timeout_s: float = 1.5
    upstream_retries: int = 2
    upstream_budget_s: float = 3.0  # hard cap on time spent on the upstream per request
    app_version: str = "dev"
    fault_injection: bool = False  # enables POST /api/_fault, for alarm drills only
    default_page_size: int = 100
    max_page_size: int = 500
    max_ingest_rows: int = 1000
    db_pool_max: int = 5
    db_pool_timeout_s: float = 5.0
    db_statement_timeout_ms: int = 5000
    max_body_bytes: int = 10 * 1024 * 1024
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        url = env.get("DATABASE_URL", "")
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        return cls(
            database_url=url,
            ingest_api_key=env.get("INGEST_API_KEY", ""),
            gpu_metrics_url=env.get("GPU_METRICS_URL", ""),
            upstream_connect_timeout_s=float(env.get("UPSTREAM_CONNECT_TIMEOUT_S", "0.5")),
            upstream_read_timeout_s=float(env.get("UPSTREAM_READ_TIMEOUT_S", "1.5")),
            upstream_retries=int(env.get("UPSTREAM_RETRIES", "2")),
            upstream_budget_s=float(env.get("UPSTREAM_BUDGET_S", "3.0")),
            app_version=env.get("APP_VERSION", "dev"),
            fault_injection=_bool(env.get("FAULT_INJECTION", "false")),
            db_pool_max=int(env.get("DB_POOL_MAX", "5")),
            log_level=env.get("LOG_LEVEL", "INFO"),
        )
