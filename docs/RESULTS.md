# Results

All data is synthetic (see `synth/generate.py`). Dates are when each result was recorded.

## Local, 2026-10-17 (Windows 11 laptop, Docker Desktop, PostgreSQL 16 in Docker)

| What | Result |
|---|---|
| Tests | 89 pytest tests pass (unit, API, database, data-quality, collector); ruff lint + format clean; shellcheck clean |
| Classifier vs planted truth (8 days, 23,772 jobs, 2,354 failures) | 99.0 % category and owner correct. Every miss is a job whose log file is missing (planted in 3 % of failures); those go to `unknown` / `human_review`, never to a wrong owner |
| Black-hole detection | planted node `cpu-087` flagged (41 of 51 jobs failed, 35 users, median runtime 22 s); no healthy node flagged; its 68 failures that day marked `node_suspect` |
| Collector catch-up | 8 runs of 24 h windows; the 5-minute overlap re-sent 1-2 jobs per run, all reported `unchanged` by the upsert |
| Volume test (`--volume-test`) | one 24 h window: 92,423 finished jobs, 10,203 failures. Parse + classify 3.97 s; 21 batches of 500 upserted in 1.0 s; collector wall time ~7 s |
| Upstream slow (5 s) | `/api/nodes/<node>/health` answered in 3.01 s with `partial: true` (3.0 s time budget) |
| Upstream down (503) | `partial: true`, warning "HTTP 503 after 3 attempt(s)" |
| Data-quality report | all six checks PASS on the 8-day data set |
| Deploy rehearsal (`deploy/test_deploy_local.sh`) | v1 deployed; v1 -> v2 upgrade; a release that crashes on startup was detected by the health check and v2 was restored automatically (deploy exit code 1) |

## AWS

Not run yet. This section will hold the dated AWS results: ingest and queries through the
API, the Lambda report, a 5xx alarm triggered on purpose, a broken deploy that rolled
back automatically, the cost of the run, and the teardown check.
