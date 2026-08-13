-- 0006: autonomous opportunity discovery storage (OC-37; spec in the scope's
-- decisions/discovery-design.md, §7). The design names this "migration 0005";
-- 0005 was taken by fact origin evidence, so discovery lands as 0006.
-- Four separated state fields on opportunities (observed availability, gate
-- verdict, proposed action, human-selected action); snapshots are immutable
-- and per-source sequenced (the ordering closure streaks and suspect cohorts
-- reference); promotion queue rows are version-pinned with durable states.

-- Source registry (§2): harvested breadth, curated relevance. source id is
-- stable identity; tenant_slug is a mutable locator. Scheduler state is
-- computed from stored DB timestamps, never local wall-clock arithmetic.
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    ats_type TEXT NOT NULL CHECK (ats_type IN
        ('greenhouse', 'lever', 'ashby', 'workable', 'smartrecruiters')),
    tenant_slug TEXT NOT NULL,
    company_name TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('harvest', 'curated', 'manual')),
    -- candidate -> enabled after one successful probe; 5 consecutive failures
    -- (config) disables; nothing is deleted, disabled re-probes slowly.
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'enabled', 'disabled')),
    last_checked TEXT,
    last_success TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_polled_at TEXT,
    next_poll_at TEXT,
    next_probe_at TEXT,
    probe_attempts INTEGER NOT NULL DEFAULT 0,
    -- Last poll outcome for CLI surfacing (§1): 'success', 'degraded',
    -- 'oversized' (feed past the per-poll page limit; auditable, resolved by
    -- raising config), or 'deferred' (budget only; touches no health state).
    last_poll_outcome TEXT CHECK (last_poll_outcome IS NULL OR
        last_poll_outcome IN ('success', 'degraded', 'oversized', 'deferred')),
    policy_notes TEXT,
    -- Optional reviewed company metadata, each field with origin provenance
    -- ('curated' or 'cli_edit'); §5 hard exclusions consume these and no
    -- classifier ever populates them silently.
    industry TEXT,
    industry_origin TEXT CHECK (industry_origin IN ('curated', 'cli_edit')),
    company_stage TEXT,
    company_stage_origin TEXT CHECK (company_stage_origin IN ('curated', 'cli_edit')),
    company_size_band TEXT,
    company_size_band_origin TEXT CHECK (company_size_band_origin IN ('curated', 'cli_edit')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT
);

CREATE UNIQUE INDEX idx_sources_ats_tenant ON sources (ats_type, tenant_slug);

-- Supersession records (§2): a company migrating ATS or renaming its tenant
-- links old source to new; a locator change is never closure plus discovery.
CREATE TABLE source_supersessions (
    id TEXT PRIMARY KEY,
    old_source_id TEXT NOT NULL REFERENCES sources (id),
    new_source_id TEXT NOT NULL REFERENCES sources (id),
    origin TEXT NOT NULL CHECK (origin IN ('migration')),
    notes TEXT,
    reviewed_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Immutable committed snapshots (§1/§3): one row per complete successful poll,
-- all pages or none. seq is the per-source committed order the absence streak
-- and suspect cohorts reference. Raw payload lives via StorageAdapter; the row
-- stores the locator and content hash, never bytes.
CREATE TABLE snapshots (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources (id),
    seq INTEGER NOT NULL,
    raw_locator TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    -- Page/cursor completion metadata proving the poll-completeness contract
    -- was met before the write landed (page count, cursor terminal state), JSON.
    completion_json TEXT NOT NULL CHECK (json_valid(completion_json)),
    posting_count INTEGER NOT NULL,
    -- Vendor snapshot/version token where offered; its presence lets closure
    -- collapse to one poll for this source, stated per adapter (§1).
    remote_version_token TEXT,
    run_id TEXT REFERENCES discovery_runs (id),
    committed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (source_id, seq)
);

-- Opportunities (§3): stable identity = (source_id, external_job_id), never
-- the mutable tenant slug. Four distinct state fields, four owners.
CREATE TABLE opportunities (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources (id),
    external_job_id TEXT NOT NULL,
    -- Observed availability: machine-owned, from polling only.
    availability TEXT NOT NULL DEFAULT 'open'
        CHECK (availability IN ('open', 'closed', 'reopened')),
    -- Consecutive absence streak over the source's committed snapshot order.
    absence_streak INTEGER NOT NULL DEFAULT 0,
    -- The snapshot that observed the current streak's first absence: the
    -- first of the two confirming snapshot ids a closure records.
    last_absence_snapshot_id TEXT REFERENCES snapshots (id),
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    closed_observed_at TEXT,
    -- Ids of the confirming snapshot(s) behind the closure, JSON array (two
    -- for the streak rule, one where a vendor version token collapses it).
    closing_snapshot_ids_json TEXT CHECK (closing_snapshot_ids_json IS NULL
        OR json_valid(closing_snapshot_ids_json)),
    -- Closed -> reopened cycles observed (§6 staleness signal: 'reposted Nx',
    -- a disclosed observation, never a verdict).
    reopen_count INTEGER NOT NULL DEFAULT 0,
    current_version_id TEXT REFERENCES opportunity_versions (id),
    -- Gate verdict: machine-owned reference to the latest auditable verdict.
    latest_gate_verdict_id TEXT REFERENCES gate_verdicts (id),
    -- Proposed action: machine-owned (IGNORE/MONITOR/pursue proposal),
    -- pinned to the version and dependency epoch it was derived from; readers
    -- suppress or mark stale a proposal whose pin is no longer current (bumps
    -- stay O(1): staleness is a read-time comparison, never a mass clear).
    proposed_action TEXT CHECK (proposed_action IN ('ignore', 'monitor', 'pursue')),
    proposed_action_version_id TEXT REFERENCES opportunity_versions (id),
    proposed_action_epoch INTEGER,
    -- Human-selected action: human-owned, never overwritten by polling.
    human_action TEXT,
    -- Workable/SmartRecruiters postings carry apply_support 'none' (§1).
    apply_support TEXT NOT NULL CHECK (apply_support IN ('extension', 'none')),
    -- Observed-ungated backlog (§4): the promotion cap gates at most N new
    -- opportunities per run; the rest wait here. Discard reason recorded when
    -- a closed or superseded observation leaves the backlog at gate time.
    backlog_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (backlog_state IN ('pending', 'gated', 'discarded')),
    backlog_discard_reason TEXT,
    -- Extracted requirement phrases persist as proposals, never verified
    -- graph edges (§5), JSON array.
    requirement_proposals_json TEXT CHECK (requirement_proposals_json IS NULL
        OR json_valid(requirement_proposals_json)),
    -- The one judged fit with a written reason (§5, OC-22), pinned to the
    -- version it judged; a newer version makes it stale, never silently reused.
    judged_fit_json TEXT CHECK (judged_fit_json IS NULL
        OR json_valid(judged_fit_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT,
    UNIQUE (source_id, external_job_id)
);

-- Version rows (§3): one per material change, append-only. Columns are the
-- material-field enumeration the fingerprint covers.
CREATE TABLE opportunity_versions (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities (id),
    version INTEGER NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES snapshots (id),
    title TEXT,
    seniority TEXT,
    work_authorization_json TEXT,
    language_requirements_json TEXT,
    description_hash TEXT,
    location_json TEXT,
    remote_policy_json TEXT,
    salary_json TEXT,
    apply_url TEXT,
    -- Deterministic hash over the material fields (domain/discovery.py).
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (opportunity_id, version)
);

-- Gate verdicts (§5): auditable per-dimension results with reasons; a
-- gated-out opportunity is stored with its reasons, never silently dropped.
-- Each verdict records the dependency epoch it was computed under.
CREATE TABLE gate_verdicts (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities (id),
    version_id TEXT NOT NULL REFERENCES opportunity_versions (id),
    epoch INTEGER NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'fail')),
    dimensions_json TEXT NOT NULL CHECK (json_valid(dimensions_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Mass-closure suspect cohorts (§3), keyed to the first triggering snapshot.
-- Membership is immutable except for row-level resolution against the
-- immediately next consecutive complete snapshot.
CREATE TABLE suspect_cohorts (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources (id),
    triggering_snapshot_id TEXT NOT NULL REFERENCES snapshots (id),
    resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE suspect_cohort_members (
    cohort_id TEXT NOT NULL REFERENCES suspect_cohorts (id),
    opportunity_id TEXT NOT NULL REFERENCES opportunities (id),
    outcome TEXT NOT NULL DEFAULT 'pending'
        CHECK (outcome IN ('pending', 'closed', 'reappeared')),
    PRIMARY KEY (cohort_id, opportunity_id)
);

-- Promotion queue (§4): version-pinned rows with durable stage states, a
-- frozen deterministic ordering key (lane rank, first_seen, opportunity id at
-- enqueue; coverage after extraction), bounded retry, terminal failed state.
CREATE TABLE promotion_queue (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities (id),
    version_id TEXT NOT NULL REFERENCES opportunity_versions (id),
    state TEXT NOT NULL DEFAULT 'pending_extraction' CHECK (state IN
        ('pending_extraction', 'extracted', 'pending_judgment', 'judged',
         'superseded', 'failed')),
    -- Frozen pre-extraction ordering key components (§4): curated lane first.
    lane_rank INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    enqueue_seq INTEGER NOT NULL UNIQUE,
    -- Coverage in basis points (integer, no floats), set at the extracted
    -- transition; judged-fit consumes extracted rows in coverage order.
    coverage_bp INTEGER,
    epoch INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    -- Run sequence before which the row is not claimable (retry backoff).
    next_attempt_run_seq INTEGER NOT NULL DEFAULT 0,
    -- Durable exclusive claim: owner token and lease fence set by one
    -- conditional update before any model call. A row claimed under a fence
    -- that is no longer the live lease is claimable again (crash recovery);
    -- a live claim blocks every other claimant.
    claimed_by TEXT,
    claimed_fence INTEGER,
    superseded_reason TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT,
    -- Epoch is part of the merge key (§5): a dependency-epoch bump must allow
    -- a fresh row for the same opportunity version; completed results from an
    -- older epoch stay as history, stale by their recorded epoch.
    UNIQUE (opportunity_id, version_id, epoch)
);

-- Run records (§4): budget locked and recorded at start, spend per stage,
-- per-source outcomes, dependency epoch, exhaustion is a safe stop.
CREATE TABLE discovery_runs (
    id TEXT PRIMARY KEY,
    run_seq INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN
        ('running', 'completed', 'budget_exhausted', 'failed')),
    exhausted_stage TEXT,
    budget_json TEXT NOT NULL CHECK (json_valid(budget_json)),
    spend_json TEXT CHECK (spend_json IS NULL OR json_valid(spend_json)),
    source_outcomes_json TEXT CHECK (source_outcomes_json IS NULL
        OR json_valid(source_outcomes_json)),
    epoch INTEGER NOT NULL,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    finished_at TEXT
);

-- Dependency epoch (§5): one integer, bumped by any audited write to user
-- policies, profile, active role families/strategy, or the eligible edge set.
-- Gate and rank results record the epoch they were computed under.
CREATE TABLE dependency_epoch (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    epoch INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT INTO dependency_epoch (id, epoch) VALUES (1, 0);

-- Query-path indexes: the per-source snapshot order (latest lookup), source
-- opportunity scans, the observed-ungated backlog sweep, and the promotion
-- queue's stage claims.
CREATE INDEX idx_snapshots_source_seq ON snapshots (source_id, seq DESC);
CREATE INDEX idx_opportunities_source ON opportunities (source_id);
CREATE INDEX idx_opportunities_backlog ON opportunities (backlog_state, first_seen);
CREATE INDEX idx_promotion_queue_stage
    ON promotion_queue (state, next_attempt_run_seq, enqueue_seq);

-- Singleton run lease (§2): the same lease discipline the package pipeline
-- uses (owner token, expiry at the database clock); concurrent runs cannot
-- double-select sources. One row, claimed by conditional update.
CREATE TABLE discovery_lease (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner_token TEXT,
    expires_at TEXT,
    -- Monotonic fence generation, bumped on every acquisition: persistent
    -- transitions verify owner+fence in-transaction, so a paused run whose
    -- lease was re-acquired can never land its writes.
    fence INTEGER NOT NULL DEFAULT 0
);

INSERT INTO discovery_lease (id) VALUES (1);
