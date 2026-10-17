from datetime import UTC, datetime, timedelta

from triage.blackhole import detect_blackholes
from triage.sacct import Job

T0 = datetime(2026, 10, 17, tzinfo=UTC)


def job(i: int, node: str, user: str, state: str = "COMPLETED", elapsed: int = 3600) -> Job:
    return Job(
        job_id=str(5000000 + i),
        job_name="x",
        user_name=user,
        account="lab01",
        partition="batch",
        state=state,
        exit_code="1:0" if state == "FAILED" else "0:0",
        start=T0,
        end=T0 + timedelta(seconds=elapsed),
        elapsed_s=elapsed,
        node_list=node,
        nodes=[node],
    )


def detect(jobs):
    return detect_blackholes(jobs, {}, T0, T0 + timedelta(days=1))


def background(n: int = 400) -> list[Job]:
    """A healthy cluster: 400 jobs over 40 nodes, ~5% failing slowly."""
    return [
        job(i, f"cpu-{i % 40:03d}", f"user{i % 30:03d}", "FAILED" if i % 20 == 0 else "COMPLETED")
        for i in range(n)
    ]


def test_flags_node_that_fails_many_users_fast():
    bad = [job(1000 + i, "cpu-087", f"user{i:03d}", "FAILED", elapsed=15) for i in range(10)]
    bad += [job(1100 + i, "cpu-087", "user050") for i in range(2)]
    alerts = detect(background() + bad)
    assert [a.node for a in alerts] == ["cpu-087"]
    a = alerts[0]
    assert (a.failed_jobs, a.total_jobs, a.distinct_users, a.median_elapsed_s) == (10, 12, 10, 15)


def test_one_users_broken_script_is_not_a_node_problem():
    same_user = [job(1000 + i, "cpu-087", "user001", "FAILED", elapsed=5) for i in range(20)]
    assert detect(background() + same_user) == []


def test_slow_failures_are_not_a_black_hole():
    slow = [job(1000 + i, "cpu-087", f"user{i:03d}", "FAILED", elapsed=3600) for i in range(10)]
    assert detect(background() + slow) == []


def test_busy_node_with_normal_failure_rate_is_fine():
    busy = [
        job(1000 + i, "cpu-001", f"user{i % 9:03d}", "FAILED" if i % 15 == 0 else "COMPLETED", 10)
        for i in range(150)
    ]
    assert detect(background() + busy) == []
