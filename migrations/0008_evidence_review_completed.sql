-- 0008: CV review completion (OC-39 resume).
-- A CV evidence row records when its review surface completed, whatever the
-- marks were. Without it, a review that rejected everything leaves no
-- surviving draft facts, which the resume path cannot tell apart from an
-- extraction that never landed its drafts: it would re-extract and re-ask a
-- CV the user has already been through. Nullable: rows created before this
-- migration carry NULL and fall back to the attributed-facts reasoning.
--
-- No backfill: for a pre-0008 row the completion cannot be reconstructed, so a
-- CV whose review genuinely produced no surviving facts is indistinguishable
-- from one interrupted before its drafts landed. Such a row is re-extracted
-- and its review asked again, which costs the user a second pass but can never
-- skip a review that never happened. Rows reviewed from 0008 onward carry the
-- stamp and are never re-asked.

ALTER TABLE evidence ADD COLUMN review_completed_at TEXT;
