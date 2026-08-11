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

## career_edges

One generic edge table carries every relationship:

`id, source_type, source_id, edge_type, target_type, target_id, claim_kind
(fact|inference), confidence NULL, provenance, derived_from_fact_id NULL,
created_by (user|import|matcher), user_verified, created_at, superseded_at NULL`.

Edges retire by `superseded_at`, never delete. A partial unique index allows one active
edge per logical tuple; the edge vocabulary (PROVES, SUPPORTS, DEMONSTRATES, REQUIRES,
PRIORITIZES) lives in `packages/domain/edges.py` and is enforced by the repository, not a
CHECK, so later phases extend it without a migration. An edge is generation-eligible only
if `user_verified = 1`, or `created_by IN ('user', 'import')` with `claim_kind = 'fact'`;
matcher-created unverified edges are proposals and are never traversed for generation.

Rows migrated from 0001 carry `'unknown'` endpoint types (their ids were untyped),
`'edge_' || old id`, the old `source` column as `provenance`, and `created_by = 'import'`.
They are excluded from traversal until re-typed; `open-career edges list --untyped`
surfaces them.

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
unchanged; the migration and its backup, once performed, stand. Note that export dumps
rows and import loads them verbatim: exported files carry personal career data and belong
under `instance/` or outside the repo, never in tracked paths (OC-26).
