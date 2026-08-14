-- Title relevance leads the frozen pre-extraction ordering key (§4).
--
-- Lane-then-recency alone spends the whole extraction and judged-fit budget on
-- whatever was polled first: with a quarter-million-row arrival-ordered backlog
-- that is one company's operations postings, and genuinely on-target roles sit
-- unread behind them. The count of distinct target-family vocabulary terms a
-- posting's TITLE matches is deterministic and free, so it decides which rows
-- earn a paid model call and in what order.
--
-- Persisted rather than recomputed, like every other component of the frozen
-- key: the ordering must be stable across runs, and a families.json edit is
-- supposed to change it only through the dependency-epoch path (0012), which
-- re-gates and re-enqueues. A recomputed score would drift silently instead.
--
-- Existing rows default to 0: they were enqueued before relevance existed, so
-- the score is unknown, not zero-by-measurement.
ALTER TABLE promotion_queue ADD COLUMN relevance_score INTEGER NOT NULL DEFAULT 0;

-- ...and unknown must not read as "cheapest, so take it anyway". Every
-- pre-existing queue row was enqueued without the relevance filter, so the
-- backlog this change exists to stop is already sitting in the queue; and
-- select_for_stage's aging half is relevance-blind by design, which is safe
-- only because zero-relevance rows never enter the queue. Leaving them would
-- hand half of every paid batch straight to them, bypassing the new filter.
--
-- Waiting for the 0012 fingerprint path to do it is not enough: on a database
-- that already ran 0012 in an earlier run, sync_families_fingerprint sees an
-- unchanged families.json and does not bump, so those rows stay CURRENT and
-- claimable. So the upgrade advances the epoch itself. The existing machinery
-- does the rest: the next run's stale-epoch sweep supersedes every queued row
-- and the stale-gate re-gate re-scores and re-enqueues the ones that qualify.
--
-- This bump arrived by an in-place edit of 0013, which reaches only databases
-- that had not yet applied it: the runner is version-keyed, so an edit to an
-- applied migration is invisible. 0015 repeats the bump as a forward migration
-- for the databases this one misses, and keeping both costs a fresh install
-- one redundant re-gate.
UPDATE dependency_epoch
   SET epoch = epoch + 1,
       updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
 WHERE id = 1;
