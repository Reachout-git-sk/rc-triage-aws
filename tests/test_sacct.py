from triage.sacct import expand_hostlist, parse_elapsed, parse_mem_mb, parse_sacct

HEADER = "JobID|JobName|User|Account|Partition|State|ExitCode|Start|End|Elapsed|Timelimit|NodeList|ReqMem|MaxRSS"


def test_parse_elapsed_formats():
    assert parse_elapsed("00:05:19") == 319
    assert parse_elapsed("1-02:03:04") == 93784
    assert parse_elapsed("05:19") == 319
    assert parse_elapsed("") is None


def test_parse_mem():
    assert parse_mem_mb("16G") == 16384
    assert parse_mem_mb("4193854K") == 4096
    assert parse_mem_mb("4000Mn") == 4000  # old sacct suffix "n" = per node
    assert parse_mem_mb("") is None


def test_expand_hostlist():
    assert expand_hostlist("cpu-[010-012,015],gpu-001") == [
        "cpu-010",
        "cpu-011",
        "cpu-012",
        "cpu-015",
        "gpu-001",
    ]
    assert expand_hostlist("cpu-001") == ["cpu-001"]
    assert expand_hostlist("None assigned") == []


def test_parse_sacct_merges_steps_and_normalizes_state():
    lines = [
        HEADER,
        "5000001|train|user001|lab02|batch|OUT_OF_MEMORY|0:125|2026-10-17T01:00:00|"
        "2026-10-17T01:10:00|00:10:00|01:00:00|cpu-[001-002]|4G|",
        "5000001.batch|batch||lab02||OUT_OF_MEMORY|0:125|2026-10-17T01:00:00|"
        "2026-10-17T01:10:00|00:10:00||cpu-001||4193854K",
        "5000001.extern|extern||lab02||COMPLETED|0:0|2026-10-17T01:00:00|"
        "2026-10-17T01:10:00|00:10:00||cpu-[001-002]||1024K",
        "5000002|eval|user002|lab03|batch|CANCELLED by 20002|0:0|2026-10-17T01:00:00|"
        "2026-10-17T01:05:00|00:05:00|01:00:00|cpu-003|4G|",
        "this|line|is|broken",
    ]
    jobs = {j.job_id: j for j in parse_sacct(lines)}
    oom = jobs["5000001"]
    assert oom.failed and oom.state == "OUT_OF_MEMORY"
    assert oom.nodes == ["cpu-001", "cpu-002"]
    assert oom.max_rss_mb == 4096 and oom.req_mem_mb == 4096
    assert oom.elapsed_s == 600 and oom.end.tzinfo is not None
    cancelled = jobs["5000002"]
    assert cancelled.state == "CANCELLED" and not cancelled.failed
    assert len(jobs) == 2
