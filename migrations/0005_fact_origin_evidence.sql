-- 0005: draft-fact provenance (OC-36 resume scoping).
-- A cv-sourced draft fact records which evidence row's extraction minted it,
-- so a resumed onboarding walks only the drafts belonging to the matched CV,
-- never another CV's. Nullable: rows created before this migration carry
-- NULL, which the resume path attributes to the sole cv evidence row when
-- exactly one exists, and refuses to guess about otherwise.

ALTER TABLE career_facts ADD COLUMN origin_evidence_id TEXT REFERENCES evidence (id);
