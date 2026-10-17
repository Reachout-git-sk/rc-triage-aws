"""Client for the (fake) GPU-metrics service, built to fail soft.

The node-health endpoint is useful even without GPU metrics, so a slow or broken
upstream must not break it. Three limits keep the endpoint fast:
  * per-attempt timeouts: (connect, read) seconds;
  * a few retries with short backoff (safe: GET has no side effects);
  * a total time budget across all attempts, so the worst case is bounded.
When everything fails we return no data plus a warning, and the endpoint answers
200 with "partial": true instead of 500.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)


@dataclass
class UpstreamResult:
    data: dict | None = None
    warning: str | None = None
    attempts: int = 0


class GpuMetricsClient:
    def __init__(
        self,
        base_url: str,
        connect_timeout_s: float = 0.5,
        read_timeout_s: float = 1.5,
        retries: int = 2,
        budget_s: float = 3.0,
        backoff_s: float = 0.2,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.connect_timeout_s = connect_timeout_s
        self.read_timeout_s = read_timeout_s
        self.retries = retries
        self.budget_s = budget_s
        self.backoff_s = backoff_s
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock

    def node_metrics(self, node: str) -> UpstreamResult:
        if not self.base_url:
            return UpstreamResult(warning="gpu_metrics: upstream not configured")
        deadline = self.clock() + self.budget_s
        problem = "no attempt made"
        made = 0  # requests actually sent
        for attempt in range(1, self.retries + 2):
            remaining = deadline - self.clock()
            if remaining <= 0.05:
                problem = f"{problem}; time budget of {self.budget_s}s used up"
                break
            made = attempt
            timeout = (min(self.connect_timeout_s, remaining), min(self.read_timeout_s, remaining))
            try:
                resp = self.session.get(f"{self.base_url}/metrics/{node}", timeout=timeout)
            except requests.Timeout:
                problem = "timed out"
            except requests.ConnectionError:
                problem = "connection failed"
            else:
                if resp.status_code == 404:
                    return UpstreamResult(attempts=attempt)  # not a GPU node: nothing to add
                if resp.status_code == 200:
                    try:
                        return UpstreamResult(data=resp.json(), attempts=attempt)
                    except ValueError:
                        problem = "invalid JSON"
                elif resp.status_code == 429 or resp.status_code >= 500:
                    problem = f"HTTP {resp.status_code}"
                else:
                    return UpstreamResult(
                        warning=f"gpu_metrics: unexpected HTTP {resp.status_code}", attempts=attempt
                    )
            log.warning(
                "gpu_metrics attempt failed",
                extra={
                    "event": "upstream_error",
                    "node": node,
                    "attempt": attempt,
                    "problem": problem,
                },
            )
            if attempt <= self.retries:
                pause = min(self.backoff_s * 2 ** (attempt - 1), max(0.0, deadline - self.clock()))
                self.sleep(pause)
        return UpstreamResult(
            warning=f"gpu_metrics unavailable: {problem} after {made} attempt(s)",
            attempts=made,
        )
