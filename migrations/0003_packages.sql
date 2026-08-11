-- 0003: package storage (OC-33; spec in the scope's
-- decisions/package-generation-design.md).
-- Lifecycle state lives on the version, not the package. Status is a plain
-- TEXT column (no CHECK): later phases add states additively
-- (GAUNTLET_RUNNING, readiness) without a migration; the repository seam
-- enforces the state-transition table and status-dependent required fields.

CREATE TABLE packages (
    id TEXT PRIMARY KEY,
    role_family_id TEXT NOT NULL REFERENCES role_families (id),
    -- Discovery seam: a future package type without a family arrives with its
    -- own explicit discriminant and identity rule, never as a NULL family.
    opportunity_id TEXT,
    approved_version_id TEXT REFERENCES package_versions (id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT
);

-- Package identity: one base package per role family.
CREATE UNIQUE INDEX idx_packages_one_base_per_family
    ON packages (role_family_id) WHERE opportunity_id IS NULL;

CREATE TABLE package_versions (
    id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL REFERENCES packages (id),
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    content_model_json TEXT,
    -- Nullable: non-null is a VERIFIED-state requirement enforced at the
    -- repository seam, not a table constraint (early failures keep NULLs).
    context_snapshot_locator TEXT,
    input_context_hash TEXT,
    verifier_report_json TEXT,
    ats_report_json TEXT,
    artifact_locator TEXT,
    artifact_hash TEXT,
    gauntlet_report_json TEXT,
    failure_report_json TEXT,
    -- Generation lease: owner token, monotonically increasing generation used
    -- as a fence in object locators, and an expiry checked at the db clock.
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (package_id, version)
);
