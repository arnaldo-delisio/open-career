-- The target families are config now (OC-42), not graph rows, so nothing in a
-- families.json edit passes through a repository that bumps dependency_epoch.
-- Without this the next run re-uses gate verdicts and coverage numbers computed
-- against the PREVIOUS families and still reports them as current: the silent
-- wrong number, not a visible failure.
--
-- The epoch row now carries a fingerprint of the validated families config.
-- A run compares it before reading the epoch and bumps on a difference, so the
-- existing stale-epoch machinery does the invalidation; there is no second
-- invalidation path. NULL means "never recorded", which the first run after
-- this migration treats as a change.

ALTER TABLE dependency_epoch ADD COLUMN families_fingerprint TEXT;
