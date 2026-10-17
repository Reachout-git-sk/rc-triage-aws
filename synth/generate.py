"""Generate a synthetic Slurm cluster: accounting records, job logs and node logs.

Everything written here is made up: node names, user names, job IDs and log text.
It lets the triage pipeline be built and tested without any real cluster data.

Output (under --out, default ./data):
  sacct/<YYYY-MM-DD>.txt    rows like `sacct --parsable2` (job, .batch and .extern lines),
                            one file per day of the job End time
  logs/jobs/slurm-<id>.out  output logs of jobs that did not complete
  logs/nodes/<node>.log     syslog-style lines (job launches, GPU Xid errors, OOM killer, ...)
  truth.csv                 the planted cause of every failed job, to score the classifier
  manifest.json             counts and the planted incidents

Usage:
  python -m synth.generate --clean                 # ~3,000 jobs/day for 1 day
  python -m synth.generate --clean --days 8        # a week of history for the volume check
  python -m synth.generate --clean --volume-test   # ~100,000 jobs/day
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

CPU_NODES = [f"cpu-{i:03d}" for i in range(1, 161)]
GPU_NODES = [f"gpu-{i:03d}" for i in range(1, 41)]
USERS = [f"user{i:03d}" for i in range(1, 251)]
# A few users submit most jobs, like on a real cluster.
USER_WEIGHTS = [1 / (rank**0.7) for rank in range(1, len(USERS) + 1)]
JOB_NAMES = ["train", "preprocess", "align_reads", "md_sim", "fit_model", "eval", "blast", "render"]
FIRST_JOB_ID = 5_000_000

# The planted problems. Times are set relative to --end in plan_incidents().
BLACKHOLE_NODE = "cpu-087"  # broken scratch mount: jobs from many users die within seconds
XID79_NODE = "gpu-013"  # GPU falls off the bus (Xid 79), node drained 40 minutes later
XID48_NODE = "gpu-027"  # double-bit ECC error (Xid 48) kills the running job
CRASH_NODES = ("cpu-021", "cpu-140")  # machine-check crash: running jobs end as NODE_FAIL
CRASH_DOWNTIME = timedelta(minutes=15)

SACCT_FIELDS = (
    "JobID",
    "JobName",
    "User",
    "Account",
    "Partition",
    "State",
    "ExitCode",
    "Start",
    "End",
    "Elapsed",
    "Timelimit",
    "NodeList",
    "ReqMem",
    "MaxRSS",
)

# Outcome weights per ~1,000 jobs. "completed" and "cancelled" are not failures.
CPU_OUTCOMES = {
    "completed": 880,
    "cancelled": 30,
    "oom": 30,
    "timeout": 25,
    "module": 10,
    "disk_quota": 8,
    "missing_file": 7,
    "segfault": 4,
    "unknown": 3,
}
GPU_OUTCOMES = {
    "completed": 840,
    "cancelled": 30,
    "cuda_oom": 45,
    "timeout": 30,
    "module": 15,
    "oom": 10,
    "cuda_assert": 10,
    "disk_quota": 5,
    "unknown": 3,
}

# What the classifier *should* say for each planted kind: (category, owner).
# Black-hole jobs look like ordinary user errors one at a time; only the
# cross-job view (many users, one node, seconds-long runs) shows the real cause.
TRUTH = {
    "oom": ("out_of_memory", "user"),
    "timeout": ("timeout", "user"),
    "module": ("missing_python_module", "user"),
    "disk_quota": ("disk_quota", "user"),
    "missing_file": ("missing_file", "user"),
    "segfault": ("segfault", "user"),
    "unknown": ("unknown", "human_review"),
    "cuda_oom": ("cuda_out_of_memory", "user"),
    "cuda_assert": ("gpu_runtime_error", "hpc_support"),
    "xid79": ("gpu_hardware", "sysadmin"),
    "xid79_after": ("gpu_hardware", "sysadmin"),
    "xid48": ("gpu_hardware", "sysadmin"),
    "node_fail": ("node_failure", "sysadmin"),
    "blackhole_file": ("missing_file", "user"),
    "blackhole_module": ("missing_python_module", "user"),
}

# (State, ExitCode) for the job line and for its .batch step.
STATES = {
    "completed": ("COMPLETED", "0:0", "COMPLETED", "0:0"),
    "cancelled": ("CANCELLED", "0:0", "CANCELLED", "0:15"),
    "oom": ("OUT_OF_MEMORY", "0:125", "OUT_OF_MEMORY", "0:125"),
    "timeout": ("TIMEOUT", "0:0", "CANCELLED", "0:15"),
    "node_fail": ("NODE_FAIL", "0:0", "NODE_FAIL", "0:0"),
    "segfault": ("FAILED", "139:0", "FAILED", "139:0"),
}
DEFAULT_FAILED_STATE = ("FAILED", "1:0", "FAILED", "1:0")

ELAPSED_RANGE = {
    "completed": (300, 6 * 3600),
    "cancelled": (10, 3 * 3600),
    "oom": (60, 2 * 3600),
    "module": (2, 20),
    "disk_quota": (600, 3 * 3600),
    "missing_file": (1, 30),
    "segfault": (60, 3600),
    "unknown": (60, 2 * 3600),
    "cuda_oom": (30, 1200),
    "cuda_assert": (60, 1800),
}

UNKNOWN_ERRORS = [
    "ERROR: solver did not converge after 500 iterations (residual=3.2e-02)",
    "FATAL: license checkout failed for feature 'solver' (code -15)",
    "Error in config: unknown key 'learning_rte' at line 42",
    "MPI_ABORT was invoked on rank 3 in communicator MPI_COMM_WORLD with errorcode 1",
    "Error: object 'counts_matrix' not found",
]
PY_MODULES = ["torch", "numpy", "pandas", "sklearn", "transformers", "scanpy", "rdkit", "Bio"]


@dataclass
class SynthJob:
    start: datetime
    elapsed_s: int
    limit_s: int
    partition: str
    nodes: list[str]
    user: str
    name: str
    req_mem_gb: int
    kind: str
    job_id: int = 0

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.elapsed_s)


@dataclass(frozen=True)
class Incidents:
    blackhole_start: datetime
    blackhole_end: datetime
    xid79_at: datetime
    xid79_drained_at: datetime
    xid48_at: datetime
    crashes: dict[str, datetime]

    def as_json(self) -> dict:
        return {
            "blackhole": {
                "node": BLACKHOLE_NODE,
                "start": fmt(self.blackhole_start),
                "end": fmt(self.blackhole_end),
            },
            "xid79": {
                "node": XID79_NODE,
                "at": fmt(self.xid79_at),
                "drained_at": fmt(self.xid79_drained_at),
            },
            "xid48": {"node": XID48_NODE, "at": fmt(self.xid48_at)},
            "node_crashes": {node: fmt(t) for node, t in self.crashes.items()},
        }


def fmt(t: datetime) -> str:
    """sacct prints times without a zone; this project treats them all as UTC."""
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def fmt_duration(seconds: int) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}-{clock}" if days else clock


def compress_hostlist(nodes: list[str]) -> str:
    """["cpu-010", "cpu-011"] -> "cpu-[010-011]" (only what this generator produces)."""
    if len(nodes) == 1:
        return nodes[0]
    prefix = nodes[0].rsplit("-", 1)[0]
    first, last = nodes[0].rsplit("-", 1)[1], nodes[-1].rsplit("-", 1)[1]
    return f"{prefix}-[{first}-{last}]"


def uid_of(user: str) -> int:
    return 20000 + int(user[4:])


def account_of(user: str) -> str:
    return f"lab{int(user[4:]) % 25 + 1:02d}"


def plan_incidents(end: datetime) -> Incidents:
    xid79_at = end - timedelta(hours=9) + timedelta(seconds=7)
    return Incidents(
        blackhole_start=end - timedelta(hours=16),
        blackhole_end=end - timedelta(hours=6),
        xid79_at=xid79_at,
        xid79_drained_at=xid79_at + timedelta(minutes=40),
        xid48_at=end - timedelta(hours=13) + timedelta(seconds=31),
        crashes={
            CRASH_NODES[0]: end - timedelta(hours=18),
            CRASH_NODES[1]: end - timedelta(hours=3),
        },
    )


def draw_elapsed(rng: random.Random, kind: str, limit_s: int) -> int:
    if kind == "timeout":
        return limit_s
    lo, hi = ELAPSED_RANGE[kind]
    hi = min(hi, limit_s - 60)
    lo = min(lo, hi)
    return rng.randint(lo, hi)


def pick_nodes(rng: random.Random, partition: str, start: datetime, inc: Incidents) -> list[str]:
    if partition == "gpu":
        # A failed GPU node finishes jobs instantly, so the scheduler keeps feeding it.
        if inc.xid79_at <= start < inc.xid79_drained_at and rng.random() < 0.3:
            return [XID79_NODE]
        return [rng.choice(GPU_NODES)]
    # The black-hole node looks idle all the time, so it attracts extra jobs.
    if inc.blackhole_start <= start < inc.blackhole_end and rng.random() < 0.05:
        return [BLACKHOLE_NODE]
    if partition == "batch" and rng.random() < 0.03:
        i = rng.randrange(0, len(CPU_NODES) - 1)
        return CPU_NODES[i : i + 2]
    return [rng.choice(CPU_NODES)]


def apply_incidents(rng: random.Random, job: SynthJob, inc: Incidents) -> None:
    """Change a job's fate when it ran on a node that had a planted problem."""
    # Nodes that are down or drained take no new jobs: the job ran somewhere else.
    for node, crashed_at in inc.crashes.items():
        if node in job.nodes and crashed_at <= job.start < crashed_at + CRASH_DOWNTIME:
            job.nodes = [rng.choice([n for n in CPU_NODES if n not in CRASH_NODES])]
    if XID79_NODE in job.nodes and job.start >= inc.xid79_drained_at:
        job.nodes = [rng.choice([n for n in GPU_NODES if n != XID79_NODE])]

    for node, crashed_at in inc.crashes.items():
        if node in job.nodes and job.start <= crashed_at < job.end:
            job.kind = "node_fail"
            # slurmctld notices the dead node a little after the crash.
            job.elapsed_s = int((crashed_at - job.start).total_seconds()) + rng.randint(30, 300)
    if XID79_NODE in job.nodes:
        if job.start <= inc.xid79_at < job.end:
            job.kind = "xid79"
            job.elapsed_s = int((inc.xid79_at - job.start).total_seconds()) + 5
        elif inc.xid79_at <= job.start < inc.xid79_drained_at:
            job.kind = "xid79_after"
            job.elapsed_s = rng.randint(5, 60)
    if XID48_NODE in job.nodes and job.start <= inc.xid48_at < job.end:
        job.kind = "xid48"
        job.elapsed_s = int((inc.xid48_at - job.start).total_seconds()) + 3
    if (
        BLACKHOLE_NODE in job.nodes
        and inc.blackhole_start <= job.start < inc.blackhole_end
        and rng.random() < 0.9
    ):
        job.kind = rng.choice(["blackhole_file", "blackhole_module"])
        job.elapsed_s = rng.randint(3, 40)
    job.elapsed_s = max(1, min(job.elapsed_s, job.limit_s))


def make_jobs(
    rng: random.Random, end: datetime, days: int, jobs_per_day: int, inc: Incidents
) -> list[SynthJob]:
    cpu_kinds, cpu_weights = list(CPU_OUTCOMES), list(CPU_OUTCOMES.values())
    gpu_kinds, gpu_weights = list(GPU_OUTCOMES), list(GPU_OUTCOMES.values())
    window_s = days * 86400
    begin = end - timedelta(seconds=window_s)
    jobs: list[SynthJob] = []
    for _ in range(days * jobs_per_day):
        start = begin + timedelta(seconds=rng.randrange(0, window_s - 600))
        roll = rng.random()
        if roll < 0.20:
            partition, limit_s = "gpu", rng.choice([2, 4, 8, 24]) * 3600
            kind = rng.choices(gpu_kinds, gpu_weights)[0]
            mem = rng.choice([32, 64, 128])
        else:
            partition = "debug" if roll < 0.25 else "batch"
            limit_s = 3600 if partition == "debug" else rng.choice([1, 2, 4, 8, 24]) * 3600
            kind = rng.choices(cpu_kinds, cpu_weights)[0]
            mem = rng.choice([4, 8, 16, 32, 64])
        job = SynthJob(
            start=start,
            elapsed_s=draw_elapsed(rng, kind, limit_s),
            limit_s=limit_s,
            partition=partition,
            nodes=pick_nodes(rng, partition, start, inc),
            user=rng.choices(USERS, USER_WEIGHTS)[0],
            name=rng.choice(JOB_NAMES),
            req_mem_gb=mem,
            kind=kind,
        )
        apply_incidents(rng, job, inc)
        if job.end > end:
            continue  # still running when this snapshot was taken: sacct has no End yet
        jobs.append(job)
    jobs.sort(key=lambda j: j.start)
    for i, job in enumerate(jobs):
        job.job_id = FIRST_JOB_ID + i
    return jobs


def sacct_rows(rng: random.Random, job: SynthJob) -> list[list[str]]:
    state, exit_code, batch_state, batch_exit = STATES.get(job.kind, DEFAULT_FAILED_STATE)
    if job.kind == "cancelled":
        state = f"CANCELLED by {uid_of(job.user)}"
    jid = str(job.job_id)
    account = account_of(job.user)
    nodelist = compress_hostlist(job.nodes)
    start, end, elapsed = fmt(job.start), fmt(job.end), fmt_duration(job.elapsed_s)
    req_kb = job.req_mem_gb * 1024 * 1024
    if job.kind == "oom":
        rss_kb = req_kb - rng.randint(0, 2048)
    else:
        rss_kb = int(req_kb * rng.uniform(0.05, 0.8))
    return [
        [
            jid,
            job.name,
            job.user,
            account,
            job.partition,
            state,
            exit_code,
            start,
            end,
            elapsed,
            fmt_duration(job.limit_s),
            nodelist,
            f"{job.req_mem_gb}G",
            "",
        ],
        [
            f"{jid}.batch",
            "batch",
            "",
            account,
            "",
            batch_state,
            batch_exit,
            start,
            end,
            elapsed,
            "",
            job.nodes[0],
            "",
            f"{rss_kb}K",
        ],
        [
            f"{jid}.extern",
            "extern",
            "",
            account,
            "",
            "COMPLETED",
            "0:0",
            start,
            end,
            elapsed,
            "",
            nodelist,
            "",
            "1024K",
        ],
    ]


def log_tail(rng: random.Random, job: SynthJob) -> list[str]:
    """The last lines a job wrote: the part the triage parser reads."""
    jid, user, node = job.job_id, job.user, job.nodes[0]
    project = f"/home/{user}/project{jid % 7}"
    script = f"/var/spool/slurmd/job{jid}/slurm_script"
    kind = job.kind
    if kind == "oom":
        return [
            "Building feature matrix ...",
            f"slurmstepd: error: Detected 1 oom_kill event in StepId={jid}.batch. Some of your "
            "processes may have been killed by the cgroup out-of-memory handler.",
        ]
    if kind == "timeout":
        return [
            f"slurmstepd: error: *** JOB {jid} ON {node} CANCELLED AT {fmt(job.end)} "
            "DUE TO TIME LIMIT ***"
        ]
    if kind == "cancelled":
        return [f"slurmstepd: error: *** JOB {jid} ON {node} CANCELLED AT {fmt(job.end)} ***"]
    if kind == "module":
        mod = rng.choice(PY_MODULES)
        return [
            "Traceback (most recent call last):",
            f'  File "{project}/{job.name}.py", line 3, in <module>',
            f"    import {mod}",
            f"ModuleNotFoundError: No module named '{mod}'",
        ]
    if kind == "disk_quota":
        if rng.random() < 0.5:
            return [f"cp: error writing '{project}/out/chunk_{jid % 97}.h5': Disk quota exceeded"]
        return [
            "Traceback (most recent call last):",
            f'  File "{project}/save.py", line 88, in <module>',
            "    np.save(out_path, arr)",
            "OSError: [Errno 122] Disk quota exceeded",
        ]
    if kind == "missing_file":
        return [
            "Traceback (most recent call last):",
            f'  File "{project}/{job.name}.py", line 12, in <module>',
            "    df = pd.read_csv(args.input)",
            f"FileNotFoundError: [Errno 2] No such file or directory: "
            f"'/scratch/{user}/data/input_{jid % 50}.csv'",
        ]
    if kind == "segfault":
        return [
            f"{script}: line 14: {rng.randint(1000, 99999)} Segmentation fault      "
            "(core dumped) ./simulate --steps 100000"
        ]
    if kind == "unknown":
        return [rng.choice(UNKNOWN_ERRORS)]
    if kind == "cuda_oom":
        if rng.random() < 0.5:
            return [
                "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB. GPU 0 has "
                "a total capacity of 39.38 GiB of which 1.02 GiB is free."
            ]
        return [
            "RuntimeError: CUDA out of memory. Tried to allocate 512.00 MiB (GPU 0; 15.78 GiB "
            "total capacity; 14.20 GiB already allocated)"
        ]
    if kind == "cuda_assert":
        return [
            "RuntimeError: CUDA error: device-side assert triggered",
            "CUDA kernel errors might be asynchronously reported at some other API call, so the "
            "stacktrace below might be incorrect.",
        ]
    if kind == "xid79":
        return ["RuntimeError: CUDA error: unspecified launch failure"]
    if kind == "xid79_after":
        return ["RuntimeError: No CUDA GPUs are available"]
    if kind == "xid48":
        return ["RuntimeError: CUDA error: uncorrectable ECC error encountered"]
    if kind == "node_fail":
        return []  # the node died: the log just stops
    chdir = (
        f"slurmstepd: error: couldn't chdir to `/scratch/{user}/run{jid % 9}': "
        "No such file or directory: going to /tmp instead"
    )
    if kind == "blackhole_file":
        return [
            chdir,
            f"python: can't open file '/tmp/{job.name}.py': [Errno 2] No such file or directory",
        ]
    if kind == "blackhole_module":
        # The user's virtualenv lives on the missing scratch mount, so imports fail.
        mod = rng.choice(PY_MODULES)
        return [
            chdir,
            "Traceback (most recent call last):",
            f"ModuleNotFoundError: No module named '{mod}'",
        ]
    raise ValueError(f"no log template for kind {kind!r}")


def write_job_log(rng: random.Random, path: Path, job: SynthJob, big: bool) -> None:
    steps = 40_000 if big else rng.randint(3, 40)
    lines = [
        f"Job {job.job_id} running on {compress_hostlist(job.nodes)} as {job.user}",
        f"Start: {fmt(job.start)}",
        "Loading environment ...",
    ]
    lines += [
        f"[{job.name}] step {i}/{steps} loss={rng.uniform(0.1, 2):.4f}" for i in range(1, steps + 1)
    ]
    lines += log_tail(rng, job)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def node_events(rng: random.Random, jobs: list[SynthJob], inc: Incidents, end: datetime, days: int):
    events: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for job in jobs:
        uid = uid_of(job.user)
        for node in job.nodes:
            events[node].append(
                (
                    job.start,
                    f"slurmd[{rng.randint(900, 4000)}]: Launching batch job {job.job_id} "
                    f"for UID {uid}",
                )
            )
        if job.kind == "oom":
            events[job.nodes[0]].append(
                (
                    job.end,
                    f"kernel: Memory cgroup out of memory: Killed process "
                    f"{rng.randint(1000, 99999)} (python) total-vm:{job.req_mem_gb * 1100000}kB, "
                    f"anon-rss:{job.req_mem_gb * 1040000}kB, UID:{uid}",
                )
            )
        if job.kind == "xid79_after":
            events[XID79_NODE].append(
                (job.start, "kernel: NVRM: GPU 0000:3b:00.0: GPU has fallen off the bus.")
            )
    events[XID79_NODE] += [
        (
            inc.xid79_at,
            f"kernel: NVRM: Xid (PCI:0000:3b:00): 79, pid={rng.randint(1000, 99999)}, "
            "name=python, GPU has fallen off the bus.",
        ),
        (inc.xid79_drained_at, "root: admin note: node drained for GPU replacement (Xid 79)"),
    ]
    events[XID48_NODE] += [
        (
            inc.xid48_at,
            f"kernel: NVRM: Xid (PCI:0000:af:00): 48, pid={rng.randint(1000, 99999)}, "
            "name=python, An uncorrectable double bit error (DBE) has been detected on "
            "GPU in the framebuffer at partition 6, subpartition 0.",
        ),
        (
            inc.xid48_at + timedelta(seconds=2),
            "kernel: NVRM: Xid (PCI:0000:af:00): 63, pid='<unknown>', "
            "Row Remapper: New row marked for remapping, reset gpu to activate.",
        ),
    ]
    for node, crashed_at in inc.crashes.items():
        events[node] += [
            (crashed_at, "kernel: mce: [Hardware Error]: Machine check events logged"),
            (
                crashed_at + timedelta(seconds=1),
                "kernel: mce: [Hardware Error]: CPU 7: Machine "
                "Check Exception: 5 Bank 4: be00000000800400",
            ),
            (crashed_at + CRASH_DOWNTIME, "kernel: Linux version 5.14.0-synthetic: booting"),
            (
                crashed_at + CRASH_DOWNTIME + timedelta(seconds=40),
                "systemd[1]: Started slurmd.service - Slurm node daemon.",
            ),
        ]
    t = inc.blackhole_start
    events[BLACKHOLE_NODE].append(
        (t, "kernel: nfs: server fs-synth-01 not responding, still trying")
    )
    while t < inc.blackhole_end:
        t += timedelta(minutes=10)
        events[BLACKHOLE_NODE].append(
            (t, "kernel: nfs: server fs-synth-01 not responding, timed out")
        )
    events[BLACKHOLE_NODE].append((inc.blackhole_end, "kernel: nfs: server fs-synth-01 OK"))
    # Background noise every ~3 hours on every node.
    begin = end - timedelta(days=days)
    for node in CPU_NODES + GPU_NODES:
        t = begin + timedelta(minutes=rng.randint(0, 180))
        while t < end:
            msg = rng.choice(
                [
                    f"systemd[1]: Started Session {rng.randint(100, 9999)} of User root.",
                    "chronyd[702]: Selected source 169.254.169.123 (synthetic)",
                    "systemd[1]: Finished dnf-makecache.service.",
                ]
            )
            events[node].append((t, msg))
            t += timedelta(minutes=rng.randint(150, 210))
    return events


def generate(
    out: Path, end: datetime, days: int, jobs_per_day: int, seed: int, big_logs: int
) -> dict:
    rng = random.Random(seed)
    inc = plan_incidents(end)
    jobs = make_jobs(rng, end, days, jobs_per_day, inc)

    sacct_dir, job_dir, node_dir = out / "sacct", out / "logs" / "jobs", out / "logs" / "nodes"
    for d in (sacct_dir, job_dir, node_dir):
        d.mkdir(parents=True, exist_ok=True)

    by_day: dict[str, list[str]] = defaultdict(list)
    for job in jobs:
        for row in sacct_rows(rng, job):
            by_day[job.end.strftime("%Y-%m-%d")].append("|".join(row))
    header = "|".join(SACCT_FIELDS)
    for day, rows in sorted(by_day.items()):
        (sacct_dir / f"{day}.txt").write_text(
            header + "\n" + "\n".join(rows) + "\n", encoding="utf-8"
        )

    not_completed = [j for j in jobs if j.kind != "completed"]
    failed = [j for j in not_completed if j.kind != "cancelled"]
    big = {j.job_id for j in rng.sample(failed, min(big_logs, len(failed)))}
    # Real life is messy: some users send output to /dev/null or delete it before
    # triage runs. Those jobs can only be classified from their Slurm state.
    no_log = {j.job_id for j in failed if j.job_id not in big and rng.random() < 0.03}
    for job in not_completed:
        if job.job_id not in no_log:
            write_job_log(rng, job_dir / f"slurm-{job.job_id}.out", job, job.job_id in big)

    for node, lines in node_events(rng, jobs, inc, end, days).items():
        lines.sort(key=lambda e: e[0])
        text = "\n".join(f"{fmt(ts)} {node} {msg}" for ts, msg in lines)
        (node_dir / f"{node}.log").write_text(text + "\n", encoding="utf-8")

    with (out / "truth.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["job_id", "kind", "category", "owner", "node", "blackhole"])
        for job in failed:
            category, owner = TRUTH[job.kind]
            writer.writerow(
                [
                    job.job_id,
                    job.kind,
                    category,
                    owner,
                    job.nodes[0],
                    int(job.kind.startswith("blackhole")),
                ]
            )

    manifest = {
        "note": "Synthetic data for a practice project. Nothing here comes from a real cluster.",
        "generated_at": fmt(datetime.now(UTC)),
        "seed": seed,
        "end": fmt(end),
        "days": days,
        "jobs_per_day_requested": jobs_per_day,
        "jobs_written": len(jobs),
        "failed_jobs": len(failed),
        "cancelled_jobs": len(not_completed) - len(failed),
        "big_logs": sorted(big),
        "missing_logs": len(no_log),
        "kinds": dict(Counter(j.kind for j in jobs).most_common()),
        "incidents": inc.as_json(),
        "nodes": len(CPU_NODES) + len(GPU_NODES),
        "users": len(USERS),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--out", type=Path, default=Path("data"))
    p.add_argument("--end", help="snapshot time, UTC YYYY-MM-DDTHH:MM:SS (default: now)")
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--jobs-per-day", type=int, default=3000)
    p.add_argument("--volume-test", action="store_true", help="~100,000 jobs/day, no big logs")
    p.add_argument("--big-logs", type=int, default=5, help="failed jobs that get a ~1.5 MB log")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--clean", action="store_true", help="delete earlier output in --out first")
    args = p.parse_args(argv)
    if args.days < 1:
        p.error("--days must be at least 1")
    if args.volume_test:
        args.jobs_per_day, args.big_logs = 100_000, 0
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.end:
        end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    else:
        end = datetime.now(UTC).replace(second=0, microsecond=0)
    out: Path = args.out
    if (out / "manifest.json").exists():
        if not args.clean:
            print(f"{out} already has generated data; add --clean to replace it", file=sys.stderr)
            return 2
        for sub in ("sacct", "logs"):
            shutil.rmtree(out / sub, ignore_errors=True)
    t0 = time.perf_counter()
    manifest = generate(out, end, args.days, args.jobs_per_day, args.seed, args.big_logs)
    secs = time.perf_counter() - t0
    print(
        f"wrote {manifest['jobs_written']:,} jobs ({manifest['failed_jobs']:,} failed) "
        f"ending by {manifest['end']} UTC to {out} in {secs:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
