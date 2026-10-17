-- One row per failed Slurm job. job_id is the primary key, which is what makes
-- ingest idempotent: sending the same job twice updates one row, never adds a second.
CREATE TABLE failures (
    job_id        text PRIMARY KEY,
    user_name     text NOT NULL,
    account       text,
    partition     text,
    node          text NOT NULL,          -- first node of the allocation
    node_list     text,
    state         text NOT NULL,          -- Slurm State: FAILED, TIMEOUT, OUT_OF_MEMORY, NODE_FAIL
    exit_code     text,
    start_time    timestamptz,
    end_time      timestamptz NOT NULL,
    elapsed_s     integer,
    req_mem_mb    integer,
    max_rss_mb    integer,
    category      text NOT NULL,
    owner         text NOT NULL CHECK (owner IN ('user', 'hpc_support', 'sysadmin', 'human_review')),
    rule          text NOT NULL,
    evidence      text,
    hint          text,
    node_suspect  boolean NOT NULL DEFAULT false,
    run_id        text NOT NULL,          -- collector run that last changed this row
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX failures_end_time_idx ON failures (end_time DESC, job_id DESC);
CREATE INDEX failures_node_idx ON failures (node, end_time DESC);
CREATE INDEX failures_category_idx ON failures (category, end_time DESC);
CREATE INDEX failures_owner_idx ON failures (owner, end_time DESC);
CREATE INDEX failures_updated_at_idx ON failures (updated_at);

-- Nodes the collector flagged as black holes for one collector window.
CREATE TABLE node_alerts (
    node             text NOT NULL,
    alert_type       text NOT NULL,
    window_start     timestamptz NOT NULL,
    window_end       timestamptz NOT NULL,
    failed_jobs      integer NOT NULL,
    total_jobs       integer NOT NULL,
    distinct_users   integer NOT NULL,
    median_elapsed_s integer NOT NULL,
    details          jsonb NOT NULL DEFAULT '{}'::jsonb,
    run_id           text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (node, alert_type, window_start)
);

CREATE INDEX node_alerts_window_end_idx ON node_alerts (window_end);

-- What each collector run says it found. The data-quality check compares job_ids
-- here with the failures table ("reconciliation") and uses received_at for freshness.
CREATE TABLE collector_runs (
    run_id          text PRIMARY KEY,
    window_start    timestamptz NOT NULL,
    window_end      timestamptz NOT NULL,
    sacct_rows      integer NOT NULL,
    failures_found  integer NOT NULL,
    failures_sent   integer NOT NULL,
    send_errors     integer NOT NULL,
    job_ids         text[] NOT NULL DEFAULT '{}',
    category_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at      timestamptz,
    finished_at     timestamptz,
    received_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX collector_runs_received_at_idx ON collector_runs (received_at);
