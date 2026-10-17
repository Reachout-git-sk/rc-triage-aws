"""The rule table: how a failed job is mapped to a category and an owner.

Each rule looks at one source of evidence:
  "log"   - the tail of the job's output log
  "state" - the Slurm State of the job (e.g. OUT_OF_MEMORY)
  "node"  - the node's own log around the time the job ran (e.g. a GPU Xid error)

Rules are tried in order and the FIRST match wins, so order is policy:
  1. Specific errors in the job's own log that are clearly the user's to fix come first.
     A job that died of "No module named torch" is the user's problem even if the GPU
     had a hiccup an hour earlier.
  2. Hardware evidence from the node beats generic symptoms, because a GPU that fell
     off the bus makes healthy code print vague "CUDA error" messages.
  3. Slurm states (OOM, TIMEOUT, NODE_FAIL) are reliable and come next.
  4. Broad patterns ("No such file or directory") come last.
Anything that matches nothing goes to "unknown" / "human_review": a person looks at it,
and if it keeps happening, someone adds a rule. The share of unknowns is a quality metric.

Owners:
  user          the researcher who ran the job can fix it (memory, time, code, paths)
  hpc_support   needs a support engineer to look with the user (consultation)
  sysadmin      the node or hardware is at fault: drain it, fix it, requeue the jobs
  human_review  no rule matched
"""

from __future__ import annotations

import re
from dataclasses import dataclass

OWNERS = frozenset({"user", "hpc_support", "sysadmin", "human_review"})
SOURCES = frozenset({"log", "state", "node"})


@dataclass(frozen=True)
class Rule:
    name: str
    source: str
    pattern: re.Pattern[str]
    category: str
    owner: str
    hint: str


def _rule(name: str, source: str, regex: str, category: str, owner: str, hint: str) -> Rule:
    assert source in SOURCES and owner in OWNERS, name  # noqa: S101 - table sanity check
    return Rule(name, source, re.compile(regex, re.MULTILINE), category, owner, hint)


_XID = r"NVRM: Xid \(PCI:[0-9A-Fa-f:.]+\): "
_OOM_HINT = "Request more memory (--mem); compare MaxRSS with ReqMem."
_TIME_HINT = "Request more time (--time) or add checkpointing so the job can resume."

RULES: tuple[Rule, ...] = (
    # 1. Clear user errors in the job log
    _rule(
        "python_module_missing",
        "log",
        r"ModuleNotFoundError: No module named '[^']+'",
        "missing_python_module",
        "user",
        "Load the right module or activate the environment that has the package.",
    ),
    _rule(
        "cuda_oom",
        "log",
        r"CUDA out of memory|torch\.OutOfMemoryError",
        "cuda_out_of_memory",
        "user",
        "Lower the batch size or request a GPU with more memory.",
    ),
    _rule(
        "disk_quota",
        "log",
        r"Disk quota exceeded|\[Errno 122\]",
        "disk_quota",
        "user",
        "Free space or write to scratch; check the quota.",
    ),
    # 2. Hardware evidence from the node's log
    _rule(
        "gpu_xid_79",
        "node",
        _XID + r"79\b|GPU has fallen off the bus",
        "gpu_hardware",
        "sysadmin",
        "GPU fell off the bus (Xid 79): drain the node, check the GPU, requeue the jobs.",
    ),
    _rule(
        "gpu_xid_48",
        "node",
        _XID + r"48\b",
        "gpu_hardware",
        "sysadmin",
        "Double-bit ECC error (Xid 48): drain the node, reset or replace the GPU.",
    ),
    _rule(
        "gpu_ecc_log",
        "log",
        r"uncorrectable ECC error",
        "gpu_hardware",
        "sysadmin",
        "GPU memory error: check the node's Xid history.",
    ),
    # 3. Slurm states
    _rule(
        "node_fail_state",
        "state",
        r"NODE_FAIL",
        "node_failure",
        "sysadmin",
        "The node died mid-job: check its logs, requeue the job.",
    ),
    _rule("oom_state", "state", r"OUT_OF_MEMORY", "out_of_memory", "user", _OOM_HINT),
    _rule(
        "oom_log",
        "log",
        r"oom[-_]kill event|Out of memory: Killed process",
        "out_of_memory",
        "user",
        _OOM_HINT,
    ),
    _rule("timeout_state", "state", r"TIMEOUT", "timeout", "user", _TIME_HINT),
    _rule("timeout_log", "log", r"DUE TO TIME LIMIT", "timeout", "user", _TIME_HINT),
    # 4. Broad patterns
    _rule(
        "cuda_runtime",
        "log",
        r"CUDA error|No CUDA GPUs are available",
        "gpu_runtime_error",
        "hpc_support",
        "CUDA failed without a clear cause: review the code and the GPU's health together.",
    ),
    _rule(
        "segfault",
        "log",
        r"Segmentation fault",
        "segfault",
        "user",
        "The program crashed: debug with a smaller input or a core dump.",
    ),
    _rule(
        "missing_file",
        "log",
        r"No such file or directory|FileNotFoundError",
        "missing_file",
        "user",
        "A path in the job does not exist on the node.",
    ),
)

UNKNOWN_CATEGORY = "unknown"
UNKNOWN_OWNER = "human_review"
UNKNOWN_HINT = "No rule matched: a person should look, then add a rule if it repeats."


def node_prefilter() -> re.Pattern[str]:
    """One regex that matches any line a "node" rule could care about (to skip noise early)."""
    return re.compile("|".join(f"(?:{r.pattern.pattern})" for r in RULES if r.source == "node"))
