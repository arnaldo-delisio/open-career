-- Aborted discovery runs persist their diagnostic (§4): a run that died after
-- 888 successful polls recorded only "run aborted by an unexpected error"
-- while the operator's terminal showed the real cause, "database is locked",
-- so the persisted record was strictly less informative than what the
-- operator saw and no one could tell a transient blip from a bug. The run row
-- now carries the failure as JSON: exception type and message, plus the stage
-- and the source id when known. It is our own exception text, never fetched
-- posting content.

ALTER TABLE discovery_runs ADD COLUMN failure_json TEXT
    CHECK (failure_json IS NULL OR json_valid(failure_json));
