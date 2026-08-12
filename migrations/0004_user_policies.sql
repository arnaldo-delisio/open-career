-- 0004: standing user policies (OC-35; spec in the scope's
-- decisions/onboarding-interview-design.md).
-- Same shape discipline as user_profile: one JSON row, a closed key set
-- validated in the domain service (packages/domain/policies.py), and every
-- write through one audited seam (adapters/storage/sqlite_policies.py).
-- Policies are system stances that derive answers (EEO stance, compensation,
-- preferences, logistics); they never hold profile-derivable form answers,
-- which stay in user_profile (OC-29).

CREATE TABLE user_policies (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    policies_json TEXT NOT NULL CHECK (json_valid(policies_json)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Audit trail mirroring profile_field_writes: old/new values are JSON-encoded
-- policy values, so a changed stance is always attributable.
CREATE TABLE policy_writes (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    source TEXT NOT NULL CHECK (source IN ('user_edit', 'resolution')),
    resolution_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
