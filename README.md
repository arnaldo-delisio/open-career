# open-career

Working repository for the open-career build: a local-first job-application system pairing a
local backend with a Chromium extension that fills application forms and stops, so that a human
performs every submission. `open-career` is the product name. Design decisions live in the
scope ledger at `../DECISIONS.md`; this repo carries only code.

## Layout

```
apps/
  api/          FastAPI app (health endpoint, repository wiring)
  cli/          open-career CLI: init, migrate, onboard, profile, edges, export, import
  extension/    placeholder (all backend calls route through one service-worker module)
packages/
  domain/       entities, ports, and domain services (traversal, extraction validation,
                profile field set); no framework or storage imports
  schemas/      canonical field schema (planned)
  prompts/      prompt assets (CV extraction)
adapters/
  storage/      SQLite repositories, migration runner, local-filesystem StorageAdapter
  models/       ModelAdapter implementations (headless Claude Code)
  sources/ browser/   planned
workers/        discovery and agent workers (planned)
migrations/     numbered SQL migrations, applied in order
templates/      document templates (planned)
scripts/        manual checks (live model smoke), never part of the test suite
tests/          unit, integration, gauntlet, browser, fixtures (captured ATS form corpus)
docs/           notes (data model, dependency justifications)
instance/       local instance data: database, backups, files. Gitignored; never tracked.
experiments/    one-off probes (Chrome Local Network Access)
```

Boundaries: domain never imports FastAPI or sqlite3; data access goes through repository
classes and filesystem access through a `StorageAdapter`, both implemented in `adapters/`.
No business logic in SQL. Before upgrading an existing database, the migration runner backs
it up into `instance/backups/` using SQLite's backup API.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```
uv sync
uv run open-career init        # create instance/ and apply migrations
uv run uvicorn apps.api.main:app   # then: curl localhost:8000/health
```

`/health` reports status plus the edge count; `/docs` serves the API reference.

Onboarding is CV-first: `open-career onboard [cv.txt]` stores the CV, extracts draft
experiences and facts through headless Claude Code (`claude -p`, subscription-backed, no
API key; the model proposes structure only and every draft is confirmed, edited, or
rejected interactively), then asks the gap questions (capabilities, goals, profile
basics). Without a CV it runs the same questions from a blank slate. Nothing the model
drafts is usable for generation until approved.

Other commands: `open-career migrate`, `open-career show` (human-readable dump of the
stored career state; `open-career profile show` for the profile alone),
`open-career profile set <field> <value>` (closed
canonical field set, every write audited), `open-career edges list [--untyped]` (untyped =
edges migrated from the 0001 schema, excluded from traversal until re-typed),
`open-career export <file.json>`, `open-career import <file.json>` (import fully replaces
table contents; schema and format in `docs/data-model.md`). Instance location defaults to
`./instance` (override with `OPEN_CAREER_INSTANCE`). Tests: `uv run pytest` (the suite
never calls the real model CLI; `scripts/smoke_claude_extraction.py` is the manual live
check).

MIT licensed. See `LICENSE`.
