"""Parse `sacct --parsable2` output into one Job per allocation.

sacct prints several lines per job: the allocation line ("5001234"), then one per
step ("5001234.batch", "5001234.extern"). The allocation line has the job's final
State; the .batch step has MaxRSS (peak memory). We merge them into one Job.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)

# Jobs in these states are failures worth triaging. COMPLETED and CANCELLED are not.
FAILURE_STATES = frozenset({"FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"})

_HOSTLIST_PART = re.compile(r"([^,\[]+(?:\[[^\]]*\])?[^,]*)")
_BRACKET = re.compile(r"^(?P<prefix>[^\[]*)\[(?P<ranges>[^\]]+)\](?P<suffix>.*)$")
_MEM = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>[KMGT]?)")
_MEM_TO_MB = {"": 1 / (1024 * 1024), "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}


@dataclass
class Job:
    job_id: str
    job_name: str
    user_name: str
    account: str
    partition: str
    state: str  # first word of State: "CANCELLED by 20028" -> "CANCELLED"
    exit_code: str
    start: datetime | None
    end: datetime | None
    elapsed_s: int | None
    node_list: str
    nodes: list[str] = field(default_factory=list)
    req_mem_mb: int | None = None
    max_rss_mb: int | None = None

    @property
    def failed(self) -> bool:
        return self.state in FAILURE_STATES


def parse_time(value: str) -> datetime | None:
    """sacct prints "2026-10-17T03:12:44" (no zone) or "Unknown"/"None"; we treat times as UTC."""
    if not value or value in ("Unknown", "None"):
        return None
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def parse_elapsed(value: str) -> int | None:
    """ "1-02:03:04" -> 93784 seconds; also handles "MM:SS" and "HH:MM:SS"."""
    if not value:
        return None
    days = 0
    if "-" in value:
        d, value = value.split("-", 1)
        days = int(d)
    parts = [int(p) for p in value.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-3:]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_mem_mb(value: str) -> int | None:
    """ "16G" -> 16384, "4193854K" -> 4095. Older sacct adds "n"/"c" (per node/cpu); ignored."""
    m = _MEM.match(value or "")
    if not m:
        return None
    return round(float(m["num"]) * _MEM_TO_MB[m["unit"]])


def expand_hostlist(hostlist: str) -> list[str]:
    """Slurm's compact node lists: "cpu-[010-012,015],gpu-001" -> five node names."""
    nodes: list[str] = []
    for part in _HOSTLIST_PART.findall(hostlist or ""):
        part = part.strip()
        if not part or part == "None assigned":
            continue
        m = _BRACKET.match(part)
        if not m:
            nodes.append(part)
            continue
        for chunk in m["ranges"].split(","):
            lo, _, hi = chunk.partition("-")
            width = len(lo)
            for n in range(int(lo), int(hi or lo) + 1):
                nodes.append(f"{m['prefix']}{n:0{width}d}{m['suffix']}")
    return nodes


def parse_sacct(lines: Iterable[str]) -> list[Job]:
    """Read sacct --parsable2 text (with its header line). Bad lines are skipped and counted."""
    it = iter(lines)
    header: list[str] | None = None
    for line in it:
        if line.strip():
            header = line.rstrip("\r\n").split("|")
            break
    if header is None:
        return []
    col = {name: i for i, name in enumerate(header)}
    jobs: dict[str, Job] = {}
    rss: dict[str, int | None] = {}
    bad = 0
    for line in it:
        line = line.rstrip("\r\n")
        if not line:
            continue
        f = line.split("|")
        if len(f) != len(header):
            bad += 1
            continue
        job_id = f[col["JobID"]]
        base, _, step = job_id.partition(".")
        try:
            if step == "batch":
                rss[base] = parse_mem_mb(f[col["MaxRSS"]])
            elif not step:
                node_list = f[col["NodeList"]]
                jobs[base] = Job(
                    job_id=base,
                    job_name=f[col["JobName"]],
                    user_name=f[col["User"]],
                    account=f[col["Account"]],
                    partition=f[col["Partition"]],
                    state=f[col["State"]].split(" ", 1)[0],
                    exit_code=f[col["ExitCode"]],
                    start=parse_time(f[col["Start"]]),
                    end=parse_time(f[col["End"]]),
                    elapsed_s=parse_elapsed(f[col["Elapsed"]]),
                    node_list=node_list,
                    nodes=expand_hostlist(node_list),
                    req_mem_mb=parse_mem_mb(f[col["ReqMem"]]),
                )
        except (ValueError, KeyError):
            bad += 1
    for base, mb in rss.items():
        if base in jobs:
            jobs[base].max_rss_mb = mb
    if bad:
        log.warning("skipped %d malformed sacct lines", bad)
    return list(jobs.values())
