import pytest

from triage.classify import classify
from triage.rules import OWNERS, RULES

XID79 = "2026-10-17T14:22:07 gpu-013 kernel: NVRM: Xid (PCI:0000:3b:00): 79, pid=2971, name=python, GPU has fallen off the bus."
XID48 = "2026-10-17T10:22:07 gpu-027 kernel: NVRM: Xid (PCI:0000:af:00): 48, pid=11, name=python, An uncorrectable double bit error (DBE) has been detected"


@pytest.mark.parametrize(
    ("state", "log", "node", "category", "owner", "rule"),
    [
        (
            "FAILED",
            "ModuleNotFoundError: No module named 'torch'",
            "",
            "missing_python_module",
            "user",
            "python_module_missing",
        ),
        (
            "FAILED",
            "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB",
            "",
            "cuda_out_of_memory",
            "user",
            "cuda_oom",
        ),
        (
            "FAILED",
            "OSError: [Errno 122] Disk quota exceeded",
            "",
            "disk_quota",
            "user",
            "disk_quota",
        ),
        (
            "FAILED",
            "RuntimeError: CUDA error: unspecified launch failure",
            XID79,
            "gpu_hardware",
            "sysadmin",
            "gpu_xid_79",
        ),
        (
            "FAILED",
            "RuntimeError: CUDA error: uncorrectable ECC error encountered",
            XID48,
            "gpu_hardware",
            "sysadmin",
            "gpu_xid_48",
        ),
        (
            "FAILED",
            "RuntimeError: CUDA error: uncorrectable ECC error encountered",
            "",
            "gpu_hardware",
            "sysadmin",
            "gpu_ecc_log",
        ),
        ("NODE_FAIL", "step 10/40 loss=0.5", "", "node_failure", "sysadmin", "node_fail_state"),
        ("OUT_OF_MEMORY", None, "", "out_of_memory", "user", "oom_state"),
        (
            "FAILED",
            "slurmstepd: error: Detected 1 oom_kill event in StepId=1.batch.",
            "",
            "out_of_memory",
            "user",
            "oom_log",
        ),
        ("TIMEOUT", None, "", "timeout", "user", "timeout_state"),
        (
            "FAILED",
            "RuntimeError: CUDA error: device-side assert triggered",
            "",
            "gpu_runtime_error",
            "hpc_support",
            "cuda_runtime",
        ),
        (
            "FAILED",
            "line 14: 4821 Segmentation fault      (core dumped) ./simulate",
            "",
            "segfault",
            "user",
            "segfault",
        ),
        (
            "FAILED",
            "FileNotFoundError: [Errno 2] No such file or directory: '/scratch/x.csv'",
            "",
            "missing_file",
            "user",
            "missing_file",
        ),
        (
            "FAILED",
            "MPI_ABORT was invoked on rank 3",
            "",
            "unknown",
            "human_review",
            "no_rule_matched",
        ),
        ("FAILED", None, "", "unknown", "human_review", "no_rule_matched"),
    ],
)
def test_rule_table(state, log, node, category, owner, rule):
    c = classify(state, log, node)
    assert (c.category, c.owner, c.rule) == (category, owner, rule)


def test_specific_user_error_beats_node_hardware_evidence():
    # The GPU had a problem, but this job died because the user's env lacks torch.
    c = classify("FAILED", "ModuleNotFoundError: No module named 'torch'", XID79)
    assert (c.category, c.owner) == ("missing_python_module", "user")


def test_node_hardware_beats_generic_cuda_symptom():
    c = classify("FAILED", "RuntimeError: No CUDA GPUs are available", XID79)
    assert c.category == "gpu_hardware"


def test_evidence_is_the_matching_line():
    log = "epoch 1\nepoch 2\nOSError: [Errno 122] Disk quota exceeded\ncleanup done\n"
    assert classify("FAILED", log).evidence == "OSError: [Errno 122] Disk quota exceeded"


def test_rule_table_is_sane():
    names = [r.name for r in RULES]
    assert len(names) == len(set(names))
    assert all(r.owner in OWNERS for r in RULES)
