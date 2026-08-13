# Data model and export format

Schema design and rationale live in the scope ledger's deep dive
(`../decisions/career-graph-schema.md`, ratified as OC-31); this file describes what the
database currently holds. All ids are TEXT, application-generated prefixed ULIDs
(`exp_01J...`); timestamps are TEXT ISO-8601 UTC.

## Entity tables (migration 0002)

| table | holds | notable constraints |
|---|---|---|
| `experiences` | containers facts hang off (role, project, education, venture, other) | `kind` CHECK enum; `start_date`/`end_date` are display labels stored as written ("September 2015" as legitimately as "2015-09"), null end = ongoing; the canonical time value behind a label comes from `packages/domain/dates.py`, which every comparison and ordering goes through |
| `career_facts` | normalized assertions, the atoms of generation | `fact_type`, `status`, `source` CHECK enums; `user_approved` gates generation use |
| `evidence` | things that back facts and capabilities | `evidence_type` CHECK enum; files live under `instance/` via StorageAdapter, the row stores a `locator`, never bytes |
| `capabilities` | operational capability model | `name` UNIQUE; `strength` is the enum none/weak/moderate/strong, never a float |
| `role_families` | target role families | `name` UNIQUE; JSON-validated `search_vocabulary`/`adjacent_titles`; no priority column (allocation in the current approved strategy version is the sole ranking authority) |
| `career_goals` | long-horizon objectives | `horizon`, `status` CHECK enums |
| `strategy_versions` | append-only versioned strategy | `version` UNIQUE; updates insert, never mutate; current = highest approved |
| `strategy_role_family_allocations` | discrete 1-to-5 emphasis per family | CHECK 1..5, UNIQUE per (version, family) |
| `user_profile` | single JSON row of the 28 canonical fields (OC-29) | `CHECK (id = 1)`, `json_valid`; closed field set validated in `packages/domain/profile.py`, which also holds the closed yes/no fields (`YES_NO_FIELDS`): synonyms are canonicalized on write, anything else is refused at the seam |
| `profile_field_writes` | audit trail of every profile mutation | appended by the one write seam (`adapters/storage/sqlite_profile.py`) |

## Policy tables (migration 0004)

Spec: the scope's `decisions/onboarding-interview-design.md` (OC-35). `user_policies` is a
single JSON row (`CHECK (id = 1)`, `json_valid`) of standing stances that *derive* answers
rather than being form answers themselves: `eeo_stance`, `compensation_floor`,
`compensation_target` (with the user's `scalar` pre-selection for single-number salary
fields), the preference policies (`company_stage_pref`, `company_size_pref`,
`industry_pref`, `work_track`, `mission_themes`), and the logistics policies
(`relocation_whitelist`, `timezone_bounds`, `visa_details`, `earliest_start`). The key set
is closed and per-key shapes are validated in `packages/domain/policies.py` (amounts and
offsets are integers, never floats, OC-22); every write goes through
`adapters/storage/sqlite_policies.py`, which appends a JSON-encoded audit row to
`policy_writes` (mirroring `profile_field_writes`). The deterministic compensation
comparison rules (fixed period factors, no currency conversion, conservative range
comparison, unknown/equity-only skip with reason) are pure functions in the same domain
module. The typed question registry generating the interview flows lives in
`packages/domain/questions.py`; a completeness test asserts every canonical field and
policy has an intentional disposition.

## career_edges

One generic edge table carries every relationship:

`id, source_type, source_id, edge_type, target_type, target_id, claim_kind
(fact|inference), confidence NULL, provenance, derived_from_fact_id NULL,
created_by (user|import|matcher), user_verified, created_at, superseded_at NULL`.

Edges retire by `superseded_at`, never delete. A partial unique index allows one active
edge per logical tuple; the edge vocabulary (PROVES, SUPPORTS, DEMONSTRATES, REQUIRES,
PRIORITIZES, TARGETS) lives in `packages/domain/edges.py` and is enforced by the repository, not a
CHECK, so later phases extend it without a migration. An edge is generation-eligible only
if `user_verified = 1`, or `created_by IN ('user', 'import')` with `claim_kind = 'fact'`;
matcher-created unverified edges are proposals and are never traversed for generation.

Rows migrated from 0001 carry `'unknown'` endpoint types (their ids were untyped),
`'edge_' || old id`, the old `source` column as `provenance`, and `created_by = 'import'`.
They are excluded from traversal until re-typed; `open-career edges list --untyped`
surfaces them.

## Package tables (migration 0003)

Spec: the scope's `decisions/package-generation-design.md` (OC-33). `packages` holds one
base package per role family (partial unique index on `role_family_id` where
`opportunity_id IS NULL`; `opportunity_id` is the discovery seam); `approved_version_id`
points only to an APPROVED version and is the only notion of "current".
`package_versions` carries the lifecycle on the version: `status` is a plain TEXT column
(later phases add states additively), the immutable audit bundle
(`content_model_json`, `context_snapshot_locator` + `input_context_hash`,
`verifier_report_json`, `ats_report_json`, `artifact_locator` + `artifact_hash`),
`failure_report_json` (always required for FAILED), `gauntlet_report_json` (**superseded**
by the Gauntlet design's append-only `gauntlet_runs` table, migration 0007: one nullable
column cannot hold re-runs under newer suites without breaking write-once or blocking
suite evolution; it stays NULL), and the generation lease (`lease_owner`,
`lease_generation`, `lease_expires_at`). The state-transition table (GENERATING to VERIFIED or FAILED,
VERIFIED to APPROVED), the status-dependent required fields, and write-once finalized
bundle fields are enforced at `adapters/storage/sqlite_packages.py`, never by convention.
Snapshot and artifact objects live under `instance/packages/<pkg>/v<N>/g<lease-gen>/`,
written once, never overwritten.

## Fact origin evidence (migration 0005)

Adds `career_facts.origin_evidence_id` (nullable FK to `evidence`): a cv-sourced draft
fact records which evidence row's extraction minted it, so a resumed onboarding walks
only the drafts belonging to the matched CV (OC-36 resume scoping).

## Discovery tables (migration 0006)

Spec: the scope's `decisions/discovery-design.md` (OC-37). All discovery state separates
machine-owned from human-owned fields structurally, never by convention.

| table | holds | notable constraints |
|---|---|---|
| `sources` | the per-tenant registry: stable `id`, mutable `tenant_slug` locator, origin (harvest/curated/manual), scheduler state (`next_poll_at`, `next_probe_at`, attempt counts, `last_poll_outcome`), reviewed company metadata with per-field origin provenance | `(ats_type, tenant_slug)` UNIQUE; status CHECK candidate/enabled/disabled; metadata origin CHECK curated/cli_edit |
| `source_supersessions` | reviewed ATS-migration/rename links between sources | origin CHECK ('migration') |
| `snapshots` | immutable committed complete polls; `seq` is the per-source order closure streaks and cohorts reference; `raw_locator` points at the captured raw page manifest under `instance/discovery/raw/` | `(source_id, seq)` UNIQUE; no update path exists |
| `opportunities` | one row per `(source_id, external_job_id)`, carrying the four separated state fields: observed availability (machine, from polling), latest gate verdict pointer (machine), proposed action with its version/epoch pin (machine), `human_action` (human, never overwritten by polling); plus the observed-ungated backlog state, absence streak, reopen count, requirement proposals, and judged fit (both version- and epoch-pinned JSON) | availability CHECK open/closed/reopened; `(source_id, external_job_id)` UNIQUE |
| `opportunity_versions` | append-only material state per posting (title, seniority, description hash, location/remote/salary JSON with provenance, apply URL) with a deterministic fingerprint | `(opportunity_id, version)` UNIQUE |
| `gate_verdicts` | every gate evaluation, appended never updated: verdict plus all nine dimension checks with reasons and skips, and the dependency epoch it ran under | verdict CHECK pass/fail |
| `suspect_cohorts` / `suspect_cohort_members` | the mass-closure guard's cohorts, keyed to the triggering snapshot, resolved row-level against the next consecutive snapshot | member outcome CHECK pending/closed/reappeared |
| `promotion_queue` | version-pinned model-stage work rows with durable states, frozen ordering keys, bounded retry, and the exclusive claim marker (`claimed_by`, `claimed_fence`) | `(opportunity_id, version_id, epoch)` UNIQUE (an epoch bump mints a fresh row); state CHECK over the six durable states |
| `discovery_runs` | one row per budgeted run: the locked budget JSON recorded at start, spend and per-source outcomes at finish, exhaustion stage | run_seq UNIQUE |
| `dependency_epoch` | the single integer bumped in-transaction by every audited write to policies, profile, strategy, or the eligible edge set; derived results record the epoch they ran under and go stale by read-time comparison | `CHECK (id = 1)` |
| `discovery_lease` | the singleton run lease: owner token, expiry at the database clock, and a monotonic `fence` bumped per acquisition; every persistent transition re-verifies owner+fence inside its transaction | `CHECK (id = 1)` |

Raw fetched bodies (every response, error documents included) persist write-once under
`instance/discovery/raw/<source>/<attempt>/response-NNNN.json` before any parsing; a
committed snapshot's manifest references the 2xx pages, and degraded poll and probe
outcomes reference every captured body from the run record.

## Gauntlet tables (migration 0007)

Spec: the scope's `decisions/gauntlet-design.md` (OC-9). `gauntlet_runs` is append-only
and write-once at the repository seam (no update, no delete): one row per judging
attempt, `seq` (the rowid alias, allocated inside the fenced completion transaction) the
sole ordering authority, `suite_version` the code identity of the invariant rules, judge
set, prompt versions, and schema versions, `complete` marking a terminal adjudication,
and locator plus hash pairs binding the write-once policy snapshot, canonical
prompt-input bytes, and raw completions under `instance/gauntlet/<run-id>/`.
`resolved_models_json` records the per-judge observed model identity (never pre-stamped
from config), and the run report additionally records `provider_versions`, the backend's
own version string observed once per run.

**Model identity is not always available, and is never invented.** The Claude Code CLI
reports a resolved model in its JSON envelope; the Codex CLI, which backs the Truth Judge,
does not: its `codex exec --json` event stream carries `thread.started`, `turn.started`,
`item.completed` and `turn.completed` only, with no model field anywhere. That judge's
identity is therefore recorded honestly as `unreported`, and what bounds the claim is the
observed provider version (for example `codex-cli 0.145.0`). A demonstration table may pin
such a judge as the literal `unreported` **only** together with an expected provider
version: the pinned identity is the pair, a change in either half voids the table until
re-demonstrated, and every record with an unreported identity carries a verbatim
limitation line naming the judge. A table must never read as though the model were known. A partial unique index on `(package_version_id, suite_version) WHERE
complete = 1` makes a second complete same-suite adjudication impossible regardless of
application logic; the **effective run** for a suite is its complete run with the
greatest `seq`. `gauntlet_reservations` gates admission (one active reservation per
version and suite, fenced: atomic claim or takeover past expiry at the database clock,
heartbeat renewal, conditional consume inside the run-insert transaction).
`approval_decisions` makes every approval an explicit record: the effective current-suite
run it cited (`gauntlet_run_id` NOT NULL), the verdict at decision time, and the override
flag with its mandatory reason; the repository owns the current suite version and refuses
approval with no effective current-suite run, override included. The `never_render`
policy key (closed set, `packages/domain/policies.py`) feeds the stage-zero
user-constraints check through the run's policy snapshot.

**Judge-call security boundary, stated precisely.** The Truth Judge shells the Codex CLI
inside a bubblewrap sandbox (`adapters/models/codex_cli.py`): only the binary's runtime
paths, a minimal CODEX_HOME holding just the auth material, and a per-call temp workdir
are mounted; without bwrap the adapter fails closed. The residual risk is that the
sandboxed agent can still read its own auth token and has network, so a prompt injection
could exfiltrate the token. Today that is acceptable because judge inputs are built
exclusively from user-approved career state (the persisted context snapshot and content
model; the runner asserts this provenance in code). TRIPWIRE: before any posting-derived
or other third-party text enters judge inputs (the discovery phase), the Codex adapter
must gain credential mediation or be replaced by a non-agentic completion endpoint;
shipping discovery-fed judges over the current adapter would hand injected text a
readable token and a network.

## Export/import

`open-career export <file.json>` dumps `{"format": "open-career-export", "version": 1,
"tables": {...}}`; export requires an initialized, fully migrated instance. `open-career
import <file.json>` proceeds in two stages. A structurally invalid dump (missing file, bad
JSON, wrong format marker or version, malformed tables mapping or rows) is rejected before
the database is touched. A valid-looking dump first migrates the target to the current
schema (with the standard pre-upgrade backup), then validates against the live schema
(exact table set, known columns) plus dump semantics: `user_profile` fields must be in
the closed canonical set, and every active typed edge must satisfy the edge vocabulary
with both endpoints present in the dump (`'unknown'`-typed legacy rows exempt). Rows then
load in one transaction with foreign keys verified before commit: import is a **full
replace** of each table's contents, all-or-nothing. A failed load leaves the data
unchanged; the migration and its backup, once performed, stand. Dump semantics also cover
`user_policies` (keys must be in the closed policy set). Note that export dumps rows and
import loads them verbatim: exported files carry personal career data and belong under
`instance/` or outside the repo, never in tracked paths (OC-26).

**JSON export is database-only, stated plainly:** evidence locators travel but their
instance files do not, so a JSON-restored instance has correct rows pointing at absent
files. The complete movable unit is the archive form: `open-career export <file.zip>`
writes `dump.json` plus `files/<locator>` for every instance file referenced by evidence
locators, package artifacts (context snapshots and rendered PDFs), and Gauntlet run
evidence (policy snapshots, prompt inputs, raw completions, each verified against its
recorded hash); URL and absolute-path locators are external references and travel as
rows only. Export refuses to
bundle a referenced file that is missing, no longer matches its recorded hash, or whose
row records no hash at all (a bundle must be hash-verifiable end to end). Import of a
`.zip` proves everything **before** anything durable changes: every bundled file verified
against its row's recorded content hash, archives missing referenced files or carrying
unreferenced ones rejected, every locator checked for containment inside the instance root
(traversal rejected), every destination checked installable, and all bytes staged to a
temp area; only then the database loads and the staged files install. A tampered,
truncated, traversing, or uninstallable bundle fails with the database and files
untouched.
