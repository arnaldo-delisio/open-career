-- Abandoned discovery runs (§4): a run killed mid-flight left its row in
-- status 'running' forever, so run history misreported what happened and any
-- tooling reading run status saw phantom live runs. The row now records the
-- lease owner and fence it ran under, which is what makes abandonment
-- provable with the lease's own ownership and expiry logic, and 'interrupted'
-- joins the terminal statuses so a reconciled run stays distinguishable from
-- a clean finish.
--
-- Table rebuild: the status CHECK cannot be widened in place, and snapshots
-- is the one table referencing discovery_runs (snapshots.run_id). Foreign
-- keys stay enforced throughout (the pragma is a no-op inside the runner's
-- transaction, and dropping a parent table under a live child reference fails
-- whether the check is immediate or deferred), so the child references are
-- parked in a mapping table, cleared, and restored onto the rebuilt parent.
-- Every step is validated by row counts before the rebuild is finished.

CREATE TABLE _0009_snapshot_runs AS
    SELECT id, run_id FROM snapshots WHERE run_id IS NOT NULL;

CREATE TABLE _0009_runs AS SELECT * FROM discovery_runs;

UPDATE snapshots SET run_id = NULL WHERE run_id IS NOT NULL;

DROP TABLE discovery_runs;

CREATE TABLE discovery_runs (
    id TEXT PRIMARY KEY,
    run_seq INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN
        ('running', 'completed', 'budget_exhausted', 'failed', 'interrupted')),
    exhausted_stage TEXT,
    budget_json TEXT NOT NULL CHECK (json_valid(budget_json)),
    spend_json TEXT CHECK (spend_json IS NULL OR json_valid(spend_json)),
    source_outcomes_json TEXT CHECK (source_outcomes_json IS NULL
        OR json_valid(source_outcomes_json)),
    epoch INTEGER NOT NULL,
    -- The lease generation this run held; NULL on rows written before this
    -- migration, which are past runs and never live ones.
    lease_owner TEXT,
    lease_fence INTEGER,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    finished_at TEXT
);

INSERT INTO discovery_runs (
    id, run_seq, status, exhausted_stage, budget_json, spend_json,
    source_outcomes_json, epoch, started_at, finished_at)
SELECT id, run_seq, status, exhausted_stage, budget_json, spend_json,
       source_outcomes_json, epoch, started_at, finished_at
FROM _0009_runs;

UPDATE snapshots SET run_id = (
    SELECT m.run_id FROM _0009_snapshot_runs m WHERE m.id = snapshots.id)
WHERE id IN (SELECT id FROM _0009_snapshot_runs);

-- Validation: every run row and every snapshot reference must have survived.
-- The CHECK fires (rolling the runner's transaction back) when either count
-- differs, so a partial rebuild can never be recorded as applied.
CREATE TABLE _0009_conversion_check (ok INTEGER NOT NULL CHECK (ok = 1));
INSERT INTO _0009_conversion_check (ok)
    SELECT (SELECT COUNT(*) FROM discovery_runs)
         = (SELECT COUNT(*) FROM _0009_runs);
INSERT INTO _0009_conversion_check (ok)
    SELECT (SELECT COUNT(*) FROM snapshots WHERE run_id IS NOT NULL)
         = (SELECT COUNT(*) FROM _0009_snapshot_runs);
DROP TABLE _0009_conversion_check;

DROP TABLE _0009_runs;
DROP TABLE _0009_snapshot_runs;
