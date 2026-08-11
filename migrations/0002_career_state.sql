-- 0002: career state entities and the typed edge layer (OC-31; spec in the
-- scope's decisions/career-graph-schema.md).
-- Entity tables carry real columns and CHECK constraints; reasoning stays in
-- services (no business logic in SQL). All ids TEXT, application-generated
-- prefixed ULIDs; timestamps TEXT ISO-8601 UTC.

CREATE TABLE experiences (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('role', 'project', 'education', 'venture', 'other')),
    title TEXT NOT NULL,
    org TEXT,
    start_date TEXT,
    end_date TEXT,
    summary TEXT,
    display_order INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT
);

CREATE TABLE career_facts (
    id TEXT PRIMARY KEY,
    experience_id TEXT REFERENCES experiences (id),
    fact_type TEXT NOT NULL CHECK
        (fact_type IN ('achievement', 'responsibility', 'skill_use', 'metric', 'scope', 'other')),
    statement TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'retracted')),
    confidence REAL,
    source TEXT NOT NULL CHECK (source IN ('cv', 'interview', 'user_edit', 'import')),
    source_location TEXT,
    user_approved INTEGER NOT NULL CHECK (user_approved IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    verified_at TEXT
);

CREATE TABLE evidence (
    id TEXT PRIMARY KEY,
    evidence_type TEXT NOT NULL CHECK (evidence_type IN
        ('cv', 'document', 'repository', 'portfolio', 'url', 'user_statement',
         'reference', 'artifact', 'metric_record')),
    title TEXT NOT NULL,
    locator TEXT,
    content_hash TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE capabilities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    strength TEXT NOT NULL CHECK (strength IN ('none', 'weak', 'moderate', 'strong')),
    last_assessed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT
);

CREATE TABLE role_families (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_order INTEGER,
    rationale TEXT NOT NULL,
    target_seniority TEXT,
    geography TEXT,
    search_vocabulary TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(search_vocabulary)),
    adjacent_titles TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(adjacent_titles)),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'dropped')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT
);

CREATE TABLE career_goals (
    id TEXT PRIMARY KEY,
    statement TEXT NOT NULL,
    horizon TEXT NOT NULL CHECK (horizon IN ('near', 'mid', 'long')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'achieved', 'dropped')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT
);

-- Append-only: updates insert a new version, never mutate. Current = highest
-- approved version.
CREATE TABLE strategy_versions (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE,
    objective TEXT NOT NULL,
    time_horizon TEXT,
    constraints_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(constraints_json)),
    hypotheses_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(hypotheses_json)),
    bottlenecks TEXT,
    focus TEXT,
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'proposal')),
    user_approved INTEGER NOT NULL CHECK (user_approved IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Discrete 1-to-5 emphasis, relational rather than JSON, the sole authority
-- for how discovery budget splits across role families (OC-22: no float weights).
CREATE TABLE strategy_role_family_allocations (
    strategy_version_id TEXT NOT NULL REFERENCES strategy_versions (id),
    role_family_id TEXT NOT NULL REFERENCES role_families (id),
    allocation INTEGER NOT NULL CHECK (1 <= allocation AND allocation <= 5),
    UNIQUE (strategy_version_id, role_family_id)
);

-- Single-row JSON profile (OC-29: 28 canonical fields, closed set validated in
-- the domain service). All mutations go through one repository operation that
-- appends to profile_field_writes; see adapters/storage/sqlite_profile.py.
CREATE TABLE user_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fields_json TEXT NOT NULL CHECK (json_valid(fields_json)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE profile_field_writes (
    id TEXT PRIMARY KEY,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    source TEXT NOT NULL CHECK (source IN ('user_edit', 'resolution')),
    resolution_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Edge layer: replace 0001's career_edges with the typed, provenance-carrying
-- shape. Data-preserving: a pre-0002 instance can legitimately hold edges (the
-- documented hand-authored import path), so every row is converted, never
-- assumed absent. 0001 ids were untyped, so endpoint types become 'unknown';
-- such edges are excluded from traversal until the user re-types them
-- (surfaced by `open-career edges list --untyped`).
ALTER TABLE career_edges RENAME TO career_edges_0001;

CREATE TABLE career_edges (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    claim_kind TEXT NOT NULL CHECK (claim_kind IN ('fact', 'inference')),
    confidence REAL,
    provenance TEXT NOT NULL,
    derived_from_fact_id TEXT REFERENCES career_facts (id),
    created_by TEXT NOT NULL CHECK (created_by IN ('user', 'import', 'matcher')),
    user_verified INTEGER NOT NULL CHECK (user_verified IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    superseded_at TEXT
);

INSERT INTO career_edges
    (id, source_type, source_id, edge_type, target_type, target_id,
     claim_kind, confidence, provenance, derived_from_fact_id,
     created_by, user_verified, created_at, superseded_at)
SELECT
    'edge_' || id, 'unknown', source_id, edge_type, 'unknown', target_id,
    claim_kind, NULL, source, NULL,
    'import', 0, created_at, NULL
FROM career_edges_0001;

-- Count validation: the conversion must have carried every row. The CHECK
-- fires (rolling back the runner's transaction) when counts differ.
CREATE TABLE _0002_conversion_check (ok INTEGER NOT NULL CHECK (ok = 1));
INSERT INTO _0002_conversion_check (ok)
    SELECT (SELECT COUNT(*) FROM career_edges)
         = (SELECT COUNT(*) FROM career_edges_0001);
DROP TABLE _0002_conversion_check;

DROP TABLE career_edges_0001;

-- One active logical edge at most; superseded copies may accumulate.
CREATE UNIQUE INDEX idx_career_edges_active_unique
    ON career_edges (source_type, source_id, edge_type, target_type, target_id)
    WHERE superseded_at IS NULL;

CREATE INDEX idx_career_edges_active_by_target
    ON career_edges (target_type, target_id, edge_type)
    WHERE superseded_at IS NULL;

CREATE INDEX idx_career_edges_active_by_source
    ON career_edges (source_type, source_id, edge_type)
    WHERE superseded_at IS NULL;
