"""Index the interesting lines of node logs so jobs can be matched to hardware events.

Node logs are mostly noise (job launches, cron, NTP). We stream each needed file once,
keep only lines that a "node" rule could match, and look them up by time with bisect.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path


class NodeEventIndex:
    def __init__(self) -> None:
        self._times: dict[str, list[datetime]] = {}
        self._lines: dict[str, list[str]] = {}

    @classmethod
    def load(
        cls, node_log_dir: Path, nodes: Iterable[str], keep: re.Pattern[str]
    ) -> NodeEventIndex:
        index = cls()
        for node in sorted(set(nodes)):
            path = node_log_dir / f"{node}.log"
            if not path.exists():
                continue
            events: list[tuple[datetime, str]] = []
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:  # one line at a time: memory stays flat for huge logs
                    if not keep.search(line):
                        continue
                    try:
                        ts = datetime.fromisoformat(line[:19]).replace(tzinfo=UTC)
                    except ValueError:
                        continue
                    events.append((ts, line.rstrip("\n")))
            events.sort(key=lambda e: e[0])
            index._times[node] = [t for t, _ in events]
            index._lines[node] = [line for _, line in events]
        return index

    def text_between(self, nodes: Iterable[str], start: datetime, end: datetime) -> str:
        """All kept lines from these nodes with start <= time <= end, joined by newlines."""
        out: list[str] = []
        for node in nodes:
            times = self._times.get(node)
            if not times:
                continue
            lo = bisect.bisect_left(times, start)
            hi = bisect.bisect_right(times, end)
            out.extend(self._lines[node][lo:hi])
        return "\n".join(out)
