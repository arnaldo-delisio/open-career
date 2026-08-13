# open-career

Working repository for the open-career build: a local-first job-application system pairing a
local backend with a Chromium extension that fills application forms and stops, so that a human
performs every submission. `open-career` is the product name. Design decisions live in the
scope ledger at `../DECISIONS.md`; this repo carries only code.

## Layout

```
apps/
  api/          FastAPI app (health endpoint, repository wiring)
  cli/          open-career CLI: init, migrate, onboard, deepen, stories, session,
                families, package, profile, policy, edges, export, import
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
  sources/      the five public job-board API adapters (host whitelist, raw
                capture, politeness policy); browser/ planned
workers/        discovery run orchestrator (workers/discovery), interview session
                workers
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
API key; the model proposes structure only), then renders one review surface listing
every extracted experience and draft fact with a stable index. Each item carries its own
mark (`1a` accept, `2r` reject, `3e` edit, ranges like `2-6a`), several per line; there is
no approve-the-remainder default, so the review ends only when every item is marked, and
rejecting an experience rejects its dependent facts visibly in the same surface (spec: the
scope's `decisions/onboarding-ux-redesign.md`, OC-39). Experiences are marked first,
because a rejected one takes its facts with it; the facts are then marked. Fact decisions
are durable per mark, so an interrupted review resumes over exactly the facts that never
got one; experience decisions become durable when the experience phase completes, so an
interrupt inside that phase asks the experiences again and the surface says so. After the review, each experience
whose confirmed facts carry no numbers gets one ask where any of them can be restated by
index (a user edit; the system never suggests a number), with facts belonging to no
experience gathered into a final group. Onboarding then asks the gap questions
(capabilities, goals, profile basics), flows into the role-families step, and asks the
must-ask block (work authorization, location/remote/relocation, notice period,
compensation floor and target). Without a CV it runs the same questions from a blank
slate. Nothing the model drafts is usable for generation until approved.

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

Each sitting can also be driven through one-shot commands (OC-36), so an agent limited to
short-lived commands can conduct it: `open-career session start <onboard|deepen|stories>
[cv]` detaches a serve process holding the sitting open, `session show` prints the
transcript since the last show plus the pending question, `session answer "<text>"`
answers it (`""` sends blank, which means skip), `session stop` terminates it. One
session at a time; persistence is per item, so a stopped or crashed session loses
nothing already persisted. Multi-prompt units (the family setup step inside onboard,
an in-progress story inside stories) persist when the unit completes; a unit cut short
is simply asked again on the next run, since resume state is computed from the data. Re-running `onboard` with the same CV file resumes from the data
itself (the review renders whatever is still unmarked, extraction never re-runs; never a
stored cursor).

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

## Discovery

Autonomous opportunity discovery (spec: the scope's `decisions/discovery-design.md`,
OC-37): a per-tenant source registry over five public, unauthenticated job-board APIs
(Greenhouse, Lever, Ashby, Workable, SmartRecruiters), polled under one budgeted run at a
time. Every poll snapshots raw responses before parsing, versions each posting on
material change, infers closure only from two consecutive complete polls (a vanished
posting is the only closure signal these APIs give), runs a deterministic policy-fed
eligibility gate with every reason and skip stored, and promotes at most a capped top
slice through two model stages (requirement extraction as verbatim posting excerpts, one
judged fit whose reason is rendered in code from those excerpts). The proposed action
defaults to IGNORE/MONITOR; nothing in discovery applies, submits, or builds a package.

**Boundary, stated plainly:** adapters call only the five whitelisted API hosts (OC-1, a
tested constant; a URL or redirect target outside it is a refused fetch), never LinkedIn,
never HTML scraping, never an authenticated endpoint. Coverage follows from that (OC-14):
public-ATS discovery reaches tech/startup hiring and structurally misses Workday, iCIMS,
Taleo, SuccessFactors, and companies posting only to job boards or their own sites; the
curated EU layer narrows, not closes, that gap. Workable and SmartRecruiters are
discovery-only (`apply_support: none`); the extension fills only Greenhouse/Lever/Ashby.
Staleness signals (days posted, reposts, description changes, salary absence) are
disclosed observations, never a score or a "ghost job" verdict (OC-13).

Quickstart:

```
open-career discover sources add greenhouse <tenant-slug> --company "Acme"
open-career discover sources enable <source-id>   # probes; enables only on success
uv run python scripts/load_curated_sources.py curated.yaml --dry-run   # curated layer
uv run python scripts/harvest_sources.py --index CC-MAIN-2026-26 --dry-run  # CC harvest
open-career discover run          # one budgeted run (see the budget note below)
open-career discover opportunities [--status open] [--gate pass|fail|none|stale]
open-career discover show <opportunity-id>
open-career discover duplicates   # report-only cross-source view, never merged
open-career discover queue list [--state failed] [--limit N]
open-career discover recover      # clears an expired run lease; a live one is refused
```

**A bare `discover run` spends real subscription model calls.** The locked budget prints
as the run's first line, before anything is spent; the defaults allow up to 30 extraction
calls and 10 judged fits (40 total model calls) per run. Cap them (or anything else) in
`instance/discovery.json`; keys and locked defaults:

| key | default | key | default |
|---|---|---|---|
| `per_host_min_interval_s` | 2 | `max_new_opportunities_gated` | 500 |
| `max_fetches` | 2000 | `max_extraction_calls` | 30 |
| `max_probes` | 2000 | `judged_fit_k` | 10 |
| `rot_threshold` | 5 | `max_total_model_calls` | 40 |
| `mass_closure_guard_percent` | 50 | `max_pages_per_poll` | 200 |
| `mass_closure_guard_min` | 10 | `default_page_cost` | 2 |
| `poll_interval_days` | 1 | `probe_backoff_base_days` | 1 |
| `probe_backoff_cap_days` | 30 | `disabled_reprobe_days` | 30 |

Set the model stages to zero (`{"max_extraction_calls": 0, "judged_fit_k": 0,
"max_total_model_calls": 0}`) for a fetch-and-gate-only run that costs no model calls.
Reviewed company metadata (`discover sources set-meta <id> industry <value>`) feeds the
gate's hard exclusions; no classifier ever fills those fields silently.

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
