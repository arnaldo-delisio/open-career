-- Repairs the epoch bump 0013 cannot be trusted to have delivered.
--
-- 0013 grew its dependency_epoch bump by an in-place edit, on the claim that
-- 0013 had never been applied anywhere. The runner records a VERSION string
-- and no content checksum (adapters/storage/migrations.py), so a database that
-- applied the ORIGINAL 0013 skips the edited file forever and never sees the
-- bump: a content change to an applied migration is invisible, and the only
-- thing that reaches every database is a forward migration. The claim is also
-- unverifiable from the repository, and at least one scratch database did run
-- the original.
--
-- Without the bump those legacy promotion_queue rows keep relevance_score = 0
-- and stay CURRENT, and select_for_stage's relevance-blind aging half hands
-- them a paid model stage: exactly the defect the relevance filter exists to
-- stop. The bump stays in 0013 as well; on a fresh install the extra bump
-- costs one redundant re-gate, which is deterministic and spends nothing.
UPDATE dependency_epoch
   SET epoch = epoch + 1,
       updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
 WHERE id = 1;
