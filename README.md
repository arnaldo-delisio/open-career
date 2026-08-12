# open-career

Working repository for the open-career build: a local-first job-application system pairing a
local backend with a Chromium extension that fills application forms and stops, so that a human
performs every submission. `open-career` is the product name. Design decisions live in the
scope ledger at `../DECISIONS.md`; this repo carries only code.

## Layout

```
apps/
  api/          FastAPI app (health endpoint, repository wiring)
  cli/          open-career CLI: init, migrate, onboard, deepen, stories, families,
                package, profile, policy, edges, export, import
  extension/    placeholder (all backend calls route through one service-worker module)
packages/
  domain/       entities, ports, and domain services (traversal, selection, CV content
                model, grounding verifier, generation pipeline, ATS check, profile and
                policy sets, question registry); no framework or storage imports
  schemas/      canonical field schema (planned)
  prompts/      prompt assets (CV extraction)
adapters/
  storage/      SQLite repositories, migration runner, local-filesystem StorageAdapter
  models/       ModelAdapter implementations (headless Claude Code)
  render/       Playwright Chromium PDF renderer, pdftotext extraction
  sources/ browser/   planned
workers/        discovery and agent workers (planned)
migrations/     numbered SQL migrations, applied in order
templates/      CV templates (single-column ATS-safe HTML; zero personal data)
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
rejected interactively), asks the gap questions (capabilities, goals, profile basics),
flows into the role-families step, then asks the must-ask block (work authorization,
location/remote/relocation, notice period, compensation floor and target). Without a CV
it runs the same questions from a blank slate. Nothing the model drafts is usable for
generation until approved, and confirming an unquantified fact offers one optional
follow-up for an honest number (a user edit; the system never suggests one).

The interview continues whenever it earns its time (spec: the scope's
`decisions/onboarding-interview-design.md`, OC-35): `open-career deepen` walks the
remaining canonical fields (links, EEO stance and fields, consents), takes additional
evidence (repos, portfolio pieces, URLs), and runs the metric catch-up pass;
`open-career stories` is the resumable depth interview, six clusters chosen from a menu
showing per-cluster completeness (story bank, capability evidence deepening, preferences
and dealbreakers, non-CV inventory, narratives, logistics), one cluster per run by
default, resume state computed from the data itself. Standing stances live in the audited
policy seam: `open-career policy set <key> <json-or-scalar>` and `open-career policy
show` (closed key set: EEO stance, compensation floor/target with scalar preference,
preference and logistics policies; deterministic comparison rules in code, OC-22).

Package generation (spec: the scope's `decisions/package-generation-design.md`, OC-33/
OC-34): `open-career families init` proposes target role families from approved state
(model proposes structure only; you confirm; a 1-to-5 emphasis and a stated objective mint
an approved strategy version, and every later allocation-affecting change mints a complete
new version), then `open-career package generate <family>` walks the evidence graph
(TARGETS to capability to SUPPORTS to evidence to PROVES to approved facts), drafts a
typed CV content model, verifies it with the deterministic grounding verifier (every
number, date, entity, and content word must trace to approved state), renders a
single-column ATS-safe PDF via headless Chromium, and runs the mandatory `pdftotext`
section-equivalence check. `package review <version>` accepts (approves) or edits with the
write-back loop: an ungrounded edit either mints the underlying fact and regenerates, or
is dropped. `package show <id>`, `package export <id> --out cv.pdf` (defaults to the
approved version, hash-validated), `package recover` (claims expired generation leases).

Other commands: `open-career migrate`, `open-career show` (human-readable dump of the
stored career state; `open-career profile show` for the profile alone),
`open-career profile set <field> <value>` (closed
canonical field set, every write audited), `open-career edges list [--untyped]` (untyped =
edges migrated from the 0001 schema, excluded from traversal until re-typed),
`open-career edges add` (interactive, vocabulary-guarded; e.g. a SUPPORTS link so the
family walk can reach evidence-backed facts),
`open-career export <file.json|file.zip>`, `open-career import <file.json|file.zip>`
(import fully replaces table contents; `.zip` bundles and restores every referenced
instance file, hash-verified; `.json` is database-only; schema and format in
`docs/data-model.md`). Instance location defaults to
`./instance` (override with `OPEN_CAREER_INSTANCE`). Tests: `uv run pytest` (the suite
never calls the real model CLI; `scripts/smoke_claude_extraction.py` is the manual live
check).

MIT licensed. See `LICENSE`.
