# open-career

Working repository for the open-career build: a local-first job-application system pairing a
local backend with a Chromium extension that fills application forms and stops, so that a human
performs every submission. `open-career` is the product name. Design decisions live in the
scope ledger at `../DECISIONS.md`; this repo carries only code.

## Layout

```
apps/
  api/          FastAPI app (health endpoint, repository wiring)
  cli/          open-career CLI: init, migrate, export, import
  extension/    placeholder (all backend calls route through one service-worker module)
packages/
  domain/       entities and ports; no framework or storage imports
  schemas/      canonical field schema (planned)
  prompts/      prompt assets (planned)
adapters/
  storage/      SQLite repositories, migration runner, local-filesystem StorageAdapter
  models/ sources/ browser/   planned
workers/        discovery and agent workers (planned)
migrations/     numbered SQL migrations, applied in order
templates/      document templates (planned)
tests/          unit, integration, gauntlet, browser, fixtures (captured ATS form corpus)
docs/           notes (dependency justifications)
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

Other commands: `open-career migrate`, `open-career export <file.json>`,
`open-career import <file.json>`. Instance location defaults to `./instance`
(override with `OPEN_CAREER_INSTANCE`). Tests: `uv run pytest`.

MIT licensed. See `LICENSE`.
