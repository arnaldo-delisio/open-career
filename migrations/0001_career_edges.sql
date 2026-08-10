-- 0001: career graph edge layer (OC-21).
-- Edges carry provenance from the first migration: claim_kind separates fact
-- from inference; source records where each edge came from. Node tables arrive
-- with the career-state schema in a later migration; ids are opaque strings
-- until then.
CREATE TABLE career_edges (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    claim_kind TEXT NOT NULL CHECK (claim_kind IN ('fact', 'inference')),
    source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX idx_career_edges_source ON career_edges (source_id);
CREATE INDEX idx_career_edges_target ON career_edges (target_id);
