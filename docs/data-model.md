# Data model and export format

Schema design and rationale live in the scope ledger's deep dive
(`../decisions/career-graph-schema.md`, ratified as OC-31); this file describes what the
database currently holds. All ids are TEXT, application-generated prefixed ULIDs
(`exp_01J...`); timestamps are TEXT ISO-8601 UTC.

## Entity tables (migration 0002)

| table | holds | notable constraints |
|---|---|---|
| `experiences` | containers facts hang off (role, project, education, venture, other) | `kind` CHECK enum |
| `career_facts` | normalized assertions, the atoms of generation | `fact_type`, `status`, `source` CHECK enums; `user_approved` gates generation use |
| `evidence` | things that back facts and capabilities | `evidence_type` CHECK enum; files live under `instance/` via StorageAdapter, the row stores a `locator`, never bytes |
| `capabilities` | operational capability model | `name` UNIQUE; `strength` is the enum none/weak/moderate/strong, never a float |
| `role_families` | target role families | `name` UNIQUE; JSON-validated `search_vocabulary`/`adjacent_titles`; no priority column (allocation in the current approved strategy version is the sole ranking authority) |
| `career_goals` | long-horizon objectives | `horizon`, `status` CHECK enums |
| `strategy_versions` | append-only versioned strategy | `version` UNIQUE; updates insert, never mutate; current = highest approved |
| `strategy_role_family_allocations` | discrete 1-to-5 emphasis per family | CHECK 1..5, UNIQUE per (version, family) |
| `user_profile` | single JSON row of the 28 canonical fields (OC-29) | `CHECK (id = 1)`, `json_valid`; closed field set validated in `packages/domain/profile.py` |
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
`failure_report_json` (always required for FAILED), `gauntlet_report_json` (the unwritten
Gauntlet seam), and the generation lease (`lease_owner`, `lease_generation`,
`lease_expires_at`). The state-transition table (GENERATING to VERIFIED or FAILED,
VERIFIED to APPROVED), the status-dependent required fields, and write-once finalized
bundle fields are enforced at `adapters/storage/sqlite_packages.py`, never by convention.
Snapshot and artifact objects live under `instance/packages/<pkg>/v<N>/g<lease-gen>/`,
written once, never overwritten.

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
locators and package artifacts (context snapshots and rendered PDFs); URL and
absolute-path locators are external references and travel as rows only. Export refuses to
bundle a referenced file that is missing, no longer matches its recorded hash, or whose
row records no hash at all (a bundle must be hash-verifiable end to end). Import of a
`.zip` proves everything **before** anything durable changes: every bundled file verified
against its row's recorded content hash, archives missing referenced files or carrying
unreferenced ones rejected, every locator checked for containment inside the instance root
(traversal rejected), every destination checked installable, and all bytes staged to a
temp area; only then the database loads and the staged files install. A tampered,
truncated, traversing, or uninstallable bundle fails with the database and files
untouched.
