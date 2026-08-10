# Data model and export format

## career_edges

The only data table so far (migration 0001). Row schema, for hand-authoring dumps:

| column | type | notes |
|---|---|---|
| `id` | integer | primary key; may be null in a dump (assigned on insert) |
| `source_id` | text | opaque node id (node tables arrive with the career-state schema) |
| `target_id` | text | opaque node id |
| `edge_type` | text | e.g. `demonstrates`, `satisfies` |
| `claim_kind` | text | must be `fact` or `inference` (CHECK-enforced) |
| `source` | text | provenance: where the edge came from |
| `created_at` | text | UTC timestamp; defaults on insert if omitted |

## Export/import

`open-career export <file.json>` dumps `{"format": "open-career-export", "version": 1,
"tables": {...}}`; export requires an initialized, fully migrated instance. `open-career
import <file.json>` proceeds in two stages. A structurally invalid dump (missing file, bad
JSON, wrong format marker or version, malformed tables mapping or rows) is rejected before
the database is touched. A valid-looking dump first migrates the target to the current
schema (with the standard pre-upgrade backup), then validates against the live schema
(exact table set, known columns) and loads rows in one transaction: import is a **full
replace** of each table's contents, all-or-nothing. A failed load leaves the data
unchanged; the migration and its backup, once performed, stand.

There is no data-entry command yet; edges are populated by the career-state layer, the next
roadmap item. Hand-authored dumps plus `import` are the interim path.
