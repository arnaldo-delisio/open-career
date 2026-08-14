-- 0010: 'unrated' joins the capability strength enum (OC-40).
-- Onboarding no longer asks the user to rate a capability it just heard them
-- name: a self-rating on one's own claimed skills carries no signal, and the
-- model-facing context now carries computed evidence depth instead. The column
-- stays, because rating deliberately later is still worth having, so the value
-- a never-rated capability holds must be sayable rather than guessed.
--
-- Table rebuild: a CHECK constraint cannot be widened in place. No index and
-- no foreign key references capabilities (career_edges points at capabilities
-- through its generic target_type/target_id pair, which carries no FK), so the
-- rebuild is the table alone. Existing rows keep their stored strengths: no
-- backfill, since a rating the user actually gave is their statement.

CREATE TABLE _0010_capabilities AS SELECT * FROM capabilities;

DROP TABLE capabilities;

CREATE TABLE capabilities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    strength TEXT NOT NULL CHECK (strength IN
        ('unrated', 'none', 'weak', 'moderate', 'strong')),
    last_assessed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT
);

INSERT INTO capabilities (
    id, name, description, strength, last_assessed_at, created_at, updated_at)
SELECT id, name, description, strength, last_assessed_at, created_at, updated_at
FROM _0010_capabilities;

-- Validation: every row must have survived the rebuild. The CHECK fires
-- (rolling the runner's transaction back) when the counts differ, so a partial
-- rebuild can never be recorded as applied.
CREATE TABLE _0010_conversion_check (ok INTEGER NOT NULL CHECK (ok = 1));
INSERT INTO _0010_conversion_check (ok)
    SELECT (SELECT COUNT(*) FROM capabilities)
         = (SELECT COUNT(*) FROM _0010_capabilities);
DROP TABLE _0010_conversion_check;

DROP TABLE _0010_capabilities;
