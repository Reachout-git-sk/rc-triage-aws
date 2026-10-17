"""The Bash collector: checkpoint, overlap, retry-by-not-advancing, stale lock."""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from synth.generate import generate

REPO = Path(__file__).resolve().parents[1]
END = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def find_bash() -> str | None:
    if os.name == "nt":
        # On Windows, prefer Git Bash: plain "bash" may be WSL, which cannot see this Python.
        git = shutil.which("git")
        if git:
            for parent in Path(git).resolve().parents:  # Git\cmd\git.exe or Git\mingw64\bin\git.exe
                candidate = parent / "bin" / "bash.exe"
                if candidate.exists():
                    return str(candidate)
        return None
    return shutil.which("bash")


BASH = find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not found")


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "data"
    generate(data, END, days=1, jobs_per_day=400, seed=11, big_logs=0)
    return {
        **os.environ,
        "PYTHON": Path(sys.executable).as_posix(),
        "SACCT_CMD": "collector/fake_sacct.sh",
        "SACCT_DATA_DIR": (data / "sacct").as_posix(),
        "JOB_LOG_DIR": (data / "logs" / "jobs").as_posix(),
        "NODE_LOG_DIR": (data / "logs" / "nodes").as_posix(),
        "STATE_DIR": (tmp_path / "state").as_posix(),
        "DRY_RUN_DIR": (tmp_path / "out").as_posix(),
        "INITIAL_LOOKBACK_HOURS": "24",
        "END_TIME": "2026-10-17T06:00:00",
    }


def collect(env: dict, **overrides) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "collector/collect.sh"],
        cwd=REPO,
        env={**env, **overrides},
        capture_output=True,
        text=True,
        timeout=120,
    )


def payload_jobs(folder: Path) -> list[str]:
    ids = []
    for f in sorted(folder.glob("*.json")):
        ids += [r["job_id"] for r in json.loads(f.read_text())["failures"]]
    return ids


def test_checkpoint_moves_only_forward_and_overlap_is_harmless(env, tmp_path):
    first = collect(env)
    assert first.returncode == 0, first.stderr
    checkpoint = tmp_path / "state" / "checkpoint"
    assert checkpoint.read_text().strip() == "2026-10-17T06:00:00"
    first_ids = payload_jobs(tmp_path / "out")
    assert first_ids, "the first window should contain failures"

    again = collect(env)
    assert again.returncode == 0 and "already up to date" in again.stderr

    shutil.rmtree(tmp_path / "out")
    later = collect(env, END_TIME="2026-10-17T12:00:00")
    assert later.returncode == 0, later.stderr
    second_ids = payload_jobs(tmp_path / "out")
    assert checkpoint.read_text().strip() == "2026-10-17T12:00:00"
    # Only the 5-minute overlap may repeat; the API's upsert makes repeats harmless.
    assert len(set(first_ids) & set(second_ids)) < 10
    summary = json.loads(sorted((tmp_path / "out").glob("*.json"))[-1].read_text())["run_summary"]
    assert summary["window_start"].startswith("2026-10-17T05:55:00")


def test_failed_send_keeps_the_checkpoint(env, tmp_path):
    env = {k: v for k, v in env.items() if k != "DRY_RUN_DIR"}
    r = collect(env, API_URL="http://127.0.0.1:9", TRIAGE_API_KEY="k", TRIAGE_MAX_ATTEMPTS="1")
    assert r.returncode == 1, r.stderr
    assert not (tmp_path / "state" / "checkpoint").exists()
    assert list((tmp_path / "state").glob("sacct-*.txt")), "rows are kept for debugging"


def test_stale_lock_is_removed(env, tmp_path):
    lock = tmp_path / "state" / "collect.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text("999999")
    r = collect(env)
    assert r.returncode == 0, r.stderr
    assert "stale lock" in r.stderr and not lock.exists()
