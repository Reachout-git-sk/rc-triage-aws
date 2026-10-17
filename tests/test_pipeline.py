"""End to end without the network: generator -> parser -> classifier -> black-hole detector."""

import csv
from datetime import UTC, datetime, timedelta

import pytest

from synth.generate import BLACKHOLE_NODE, TRUTH, generate, parse_args
from triage.pipeline import analyze

END = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("synth")
    manifest = generate(out, END, days=1, jobs_per_day=3000, seed=7, big_logs=2)
    files = sorted((out / "sacct").glob("*.txt"))
    combined = out / "all.txt"
    lines = []
    for i, f in enumerate(files):
        content = f.read_text(encoding="utf-8").splitlines()
        lines += content if i == 0 else content[1:]
    combined.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out, manifest, combined


def test_generator_plants_every_kind(dataset):
    _, manifest, _ = dataset
    for kind in (
        "oom",
        "timeout",
        "module",
        "cuda_oom",
        "disk_quota",
        "xid79",
        "xid48",
        "blackhole_file",
        "blackhole_module",
    ):
        assert manifest["kinds"].get(kind, 0) > 0, kind
    assert set(manifest["kinds"]) - {"completed", "cancelled"} <= set(TRUTH)


def test_generator_is_deterministic(tmp_path):
    a = generate(tmp_path / "a", END, 1, 500, seed=3, big_logs=0)
    b = generate(tmp_path / "b", END, 1, 500, seed=3, big_logs=0)
    assert a["kinds"] == b["kinds"]
    assert (tmp_path / "a" / "truth.csv").read_text() == (tmp_path / "b" / "truth.csv").read_text()


def test_volume_flag():
    assert parse_args(["--volume-test"]).jobs_per_day == 100_000


def test_classifier_matches_ground_truth_and_finds_the_black_hole(dataset):
    out, _, combined = dataset
    result = analyze(
        combined, out / "logs" / "jobs", out / "logs" / "nodes", "t", END - timedelta(days=1), END
    )
    with (out / "truth.csv").open() as fh:
        truth = {r["job_id"]: r for r in csv.DictReader(fh)}
    assert len(result.records) == len(truth)
    right = sum(
        1
        for r in result.records
        if (r["category"], r["owner"])
        == (truth[r["job_id"]]["category"], truth[r["job_id"]]["owner"])
    )
    # Not 100 %: ~3 % of failed jobs have no log file and fall back to their Slurm state
    # or to "unknown". Those must go to a human, never to a wrong owner.
    assert right / len(truth) >= 0.95
    for r in result.records:
        t = truth[r["job_id"]]
        assert (r["category"], r["owner"]) == (t["category"], t["owner"]) or r[
            "owner"
        ] == "human_review"
    assert BLACKHOLE_NODE in [a.node for a in result.alerts]
    suspects = {r["node"] for r in result.records if r["node_suspect"]}
    assert BLACKHOLE_NODE in suspects
