-- 0007: the Gauntlet judging loop (OC-9; spec in the scope's
-- decisions/gauntlet-design.md). The design document numbers this migration
-- 0006; 0006 shipped as discovery before this build landed, so the content
-- lands here unchanged under the next free number.
-- Runs are append-only rows in gauntlet_runs (the 0003 gauntlet_report_json
-- column is superseded by this design and stays NULL); approvals become
-- explicit decision records; admission is gated by a small fenced
-- reservation table (atomic claim, heartbeat renewal, conditional consume in
-- the run-insert transaction).

CREATE TABLE gauntlet_runs (
    -- SQLite rowid alias: a database-enforced unique monotonic key allocated
    -- by the insert inside the fenced completion transaction; the sole
    -- ordering authority (timestamps are display only).
    seq INTEGER PRIMARY KEY,
    id TEXT UNIQUE NOT NULL,
    package_version_id TEXT NOT NULL REFERENCES package_versions (id),
    suite_version TEXT NOT NULL,
    -- Display label only, never an ordering rule.
    attempt INTEGER NOT NULL,
    -- 1 when the run reached a terminal adjudication (stage-zero FAIL, any
    -- valid judge FAIL, or every judge terminal); 0 leaves the suite
    -- re-runnable (operational abstention).
    complete INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    prompt_inputs_locator TEXT NOT NULL,
    prompt_inputs_hash TEXT NOT NULL,
    raw_completions_locator TEXT NOT NULL,
    raw_completions_hash TEXT NOT NULL,
    resolved_models_json TEXT NOT NULL,
    policy_snapshot_locator TEXT NOT NULL,
    policy_snapshot_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (package_version_id, suite_version, attempt)
);

-- The database backstop that makes a second complete same-suite adjudication
-- impossible regardless of application logic.
CREATE UNIQUE INDEX idx_gauntlet_one_complete_per_suite
    ON gauntlet_runs (package_version_id, suite_version) WHERE complete = 1;

CREATE TABLE approval_decisions (
    id TEXT PRIMARY KEY,
    package_version_id TEXT NOT NULL REFERENCES package_versions (id),
    -- NOT NULL: an approval always records a real run; an override can waive
    -- a recorded verdict, never missing adjudication.
    gauntlet_run_id TEXT NOT NULL REFERENCES gauntlet_runs (id),
    verdict_at_decision TEXT NOT NULL,
    override INTEGER NOT NULL,
    override_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- One active reservation per (package_version_id, suite_version): fenced
-- admission for a run attempt. Claim is one atomic conditional operation
-- (insert, or take over only past expiry at the database clock, minting a
-- new owner token); renewal and consume both match owner token and unexpired
-- expiry, so an expired worker can never establish the effective
-- adjudication or drop a successor's reservation.
CREATE TABLE gauntlet_reservations (
    package_version_id TEXT NOT NULL REFERENCES package_versions (id),
    suite_version TEXT NOT NULL,
    owner_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (package_version_id, suite_version)
);
