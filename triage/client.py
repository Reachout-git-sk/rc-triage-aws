"""Send triage results to the API, retrying safely.

Why retrying a POST is safe here: the API upserts by job_id. If a request times out
after the server already saved the rows, sending the same batch again changes nothing.
Without that idempotency, a retry could double-count failures.

Retry policy:
  * retry on connection errors, timeouts, 429 and 5xx (temporary problems)
  * never retry other 4xx (bad key, bad payload): retrying cannot fix them
  * exponential backoff with full jitter (random 0..cap), so many collectors that
    failed together do not all retry at the same moment; honor Retry-After
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

import requests

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class IngestError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class IngestClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: tuple[float, float] = (3.05, 30.0),  # (connect, read) seconds
        max_attempts: int = 5,
        base_delay_s: float = 0.5,
        max_delay_s: float = 8.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.url = base_url.rstrip("/") + "/api/ingest"
        self.api_key = api_key
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.session = session or requests.Session()
        self.sleep = sleep

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        cap = min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1))
        delay = random.uniform(0, cap)
        if retry_after and retry_after.isdigit():
            delay = max(delay, min(float(retry_after), self.max_delay_s * 4))
        return delay

    def post(self, payload: dict) -> dict:
        headers = {"X-API-Key": self.api_key}
        for attempt in range(1, self.max_attempts + 1):
            retry_after = None
            try:
                resp = self.session.post(
                    self.url, json=payload, headers=headers, timeout=self.timeout
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                problem = f"{type(exc).__name__}"
            else:
                if resp.status_code < 300:
                    return resp.json()
                if resp.status_code not in RETRYABLE_STATUS:
                    raise IngestError(
                        f"HTTP {resp.status_code}: {resp.text[:300]}", status=resp.status_code
                    )
                problem = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
            if attempt == self.max_attempts:
                raise IngestError(f"gave up after {attempt} attempts: {problem}", retryable=True)
            delay = self._delay(attempt, retry_after)
            log.warning("ingest attempt %d failed (%s); retrying in %.2fs", attempt, problem, delay)
            self.sleep(delay)
        raise AssertionError("unreachable")
