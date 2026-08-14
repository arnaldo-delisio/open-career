"""Independent spec-derived end-to-end tests (OC-37,
decisions/discovery-design.md), authored from the ratified design only.
They drive run_discovery against a tmp sqlite instance with canned transports
and a fake model, pinning invariants across runs that unit tests cannot see:
non-consecutive absences never closing, confirming-snapshot ids being the
consecutive pair, cohort timing with reappearance, the locked budget recorded
whole on the run row, exhaustion resuming with nothing lost and nothing
double-processed, closure superseding pending queue work, backlog promotion
discarding closed observations with the reason, and a per-snapshot id
collision committing nothing. Companion unit file:
tests/unit/test_discovery_spec_independent.py.
"""

import json
import sqlite3

import pytest

from adapters.sources.greenhouse import GreenhouseAdapter
from adapters.sources.http import HttpFetcher
from adapters.storage.local import LocalStorageAdapter
from adapters.storage.migrations import migrate
from adapters.storage.sqlite_discovery import (
    SqliteSnapshotRepository,
    SqliteSourceRegistryRepository,
)
from adapters.storage.sqlite_opportunities import (
    SqliteOpportunityRepository,
    SqlitePromotionQueueRepository,
)
from domain.budget import Budget
from domain.discovery import Source
from domain.ids import new_id
from domain.ports import ModelAdapter
from workers.discovery.run import DiscoveryConfig, run_discovery


def gh_job(job_id: int, title: str = "Engineer",
           content: str = "Requirements: Python, SQL.") -> dict:
    return {"id": job_id, "title": f"{title} {job_id}", "content": content,
            "location": {"name": "Milan, Italy"},
            "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}"}


def gh_board(*jobs) -> dict:
    return {"jobs": list(jobs), "meta": {"total": len(jobs)}}


class CannedTransport:
    def __init__(self, board: dict):
        self.board = board

    def __call__(self, url: str, headers: dict, timeout: float):
        return 200, json.dumps(self.board).encode()


class CountingModel(ModelAdapter):
    """Schema-valid output for both stages; counts calls per stage across
    runs so double-processing is observable."""

    def __init__(self):
        self.extraction_prompts: list[str] = []
        self.judgment_prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        if "extracting the stated requirements" in prompt:
            self.extraction_prompts.append(prompt)
            return json.dumps({"requirements": ["Python", "SQL"]})
        self.judgment_prompts.append(prompt)
        return json.dumps({"fit": "low", "matched_requirement_ids": [],
                           "gap_requirement_ids": ["r1", "r2"]})


@pytest.fixture
def env(tmp_path):
    migrate(tmp_path / "open-career.sqlite3")
    conn = sqlite3.connect(tmp_path / "open-career.sqlite3")
    conn.execute("PRAGMA foreign_keys = ON")
    storage = LocalStorageAdapter(tmp_path)
    # Placeholder target families: families.json is required for every run
    # (OC-42) and is the run's coverage vocabulary.
    storage.write_text("families.json", json.dumps({"families": [{
        "name": "Example Platform Family", "seniority": "senior",
        "search_vocabulary": ["Python"], "adjacent_titles": []}]}))
    source = Source(id=new_id("src"), ats_type="greenhouse",
                    tenant_slug="acme", origin="curated")
    registry = SqliteSourceRegistryRepository(conn)
    registry.add(source)
    registry.set_status(source.id, "enabled")
    transport = CannedTransport(gh_board())
    fetcher = HttpFetcher(transport=transport, sleep=lambda _s: None,
                          clock=lambda: 0.0, min_interval_s=0)
    adapters = {"greenhouse": GreenhouseAdapter(fetcher)}
    model = CountingModel()
    yield conn, storage, source, transport, adapters, model
    conn.close()


def run_once(env_tuple, budget: Budget | None = None):
    conn, storage, _source, _transport, adapters, model = env_tuple
    config = DiscoveryConfig(budget=budget or Budget(), poll_interval_days=0)
    return run_discovery(conn, storage, model, adapters, config=config,
                         say=lambda *_a, **_k: None)


def opportunity_by_external_id(conn, source_id: str, external_id: str):
    return next(o for o in SqliteOpportunityRepository(conn)
                .list_for_source(source_id)
                if o.external_job_id == external_id)


# ------------------------------------------------------------- closure (§3)

def test_non_consecutive_absences_never_close_and_confirming_ids_are_the_consecutive_pair(env):
    """§3: any presence resets the streak, and closure fires only when the two
    confirming snapshots are consecutive in committed order. Absent, present,
    absent must not close; when the posting then closes on a fourth poll, the
    closing record must name the LAST two snapshots, not the pre-presence
    absence."""
    conn, _storage, source, transport, _adapters, _model = env
    transport.board = gh_board(gh_job(1), gh_job(2))
    run_once(env)  # snapshot 1: both present

    transport.board = gh_board(gh_job(1))
    run_once(env)  # snapshot 2: job 2 absent (streak 1)
    transport.board = gh_board(gh_job(1), gh_job(2))
    run_once(env)  # snapshot 3: job 2 present again
    transport.board = gh_board(gh_job(1))
    run_once(env)  # snapshot 4: absent again: streak must be 1, not 2

    opp = opportunity_by_external_id(conn, source.id, "2")
    assert opp.availability == "open"  # absent-present-absent never closes
    assert opp.absence_streak == 1

    run_once(env)  # snapshot 5: second consecutive absence closes
    opp = opportunity_by_external_id(conn, source.id, "2")
    assert opp.availability == "closed"
    snapshots = SqliteSnapshotRepository(conn).list_for_source(source.id)
    by_id = {s.id: s.seq for s in snapshots}
    confirming = json.loads(opp.closing_snapshot_ids_json)
    assert [by_id[i] for i in confirming] == [4, 5]  # the consecutive pair


def test_cohort_closes_after_exactly_one_confirming_poll_with_reappearance_and_a_later_loner(env):
    """§3 mass-closure timing end to end: the suspect cohort forms on the
    triggering snapshot, members still absent in the immediately next
    consecutive snapshot close (confirming ids = trigger + next), members
    that reappear leave the cohort unclosed, and a posting disappearing after
    the cohort resolved follows the ordinary two-absence streak, never the
    cohort's fate."""
    conn, _storage, source, transport, _adapters, _model = env
    jobs = {i: gh_job(i) for i in range(1, 13)}
    transport.board = gh_board(*jobs.values())
    run_once(env)  # snapshot 1: 12 present

    transport.board = gh_board(jobs[1])  # 11 of 12 vanish: > 50%, >= 10
    run_once(env)  # snapshot 2: cohort forms, NOTHING closes yet
    repo = SqliteOpportunityRepository(conn)
    assert all(o.availability == "open" for o in repo.list_for_source(source.id))

    transport.board = gh_board(jobs[1], jobs[3], jobs[4])
    run_once(env)  # snapshot 3: confirming poll; 3 and 4 reappear
    snapshots = SqliteSnapshotRepository(conn).list_for_source(source.id)
    by_id = {s.id: s.seq for s in snapshots}
    for external_id in ("2", "5", "6", "7", "8", "9", "10", "11", "12"):
        opp = opportunity_by_external_id(conn, source.id, external_id)
        assert opp.availability == "closed", external_id  # exactly one confirming poll
        confirming = json.loads(opp.closing_snapshot_ids_json)
        assert [by_id[i] for i in confirming] == [2, 3]  # trigger + next
    for external_id in ("1", "3", "4"):
        opp = opportunity_by_external_id(conn, source.id, external_id)
        assert opp.availability == "open", external_id
    assert repo.pending_cohort_for_source(source.id) is None  # resolved

    transport.board = gh_board(jobs[3], jobs[4])  # job 1 vanishes alone
    run_once(env)  # snapshot 4
    opp = opportunity_by_external_id(conn, source.id, "1")
    assert opp.availability == "open"  # ordinary streak, not cohort timing
    assert opp.absence_streak == 1
    run_once(env)  # snapshot 5: second consecutive absence
    opp = opportunity_by_external_id(conn, source.id, "1")
    assert opp.availability == "closed"
    confirming = json.loads(opp.closing_snapshot_ids_json)
    by_id = {s.id: s.seq
             for s in SqliteSnapshotRepository(conn).list_for_source(source.id)}
    assert [by_id[i] for i in confirming] == [4, 5]


# -------------------------------------------------------------- budget (§4)

def test_locked_budget_is_recorded_whole_on_the_run_row(env):
    """§4: the per-run budget is locked in config before the run and recorded
    on the run row: every budget field, not a summary."""
    conn, _storage, _source, transport, _adapters, _model = env
    transport.board = gh_board(gh_job(1))
    budget = Budget(max_fetches=77, max_probes=11, max_extraction_calls=5,
                    judged_fit_k=2, max_new_opportunities_gated=9,
                    max_total_model_calls=6, rot_threshold=4,
                    mass_closure_guard_percent=60, mass_closure_guard_min=12,
                    per_host_min_interval_s=3)
    run = run_once(env, budget=budget)
    recorded = json.loads(run.budget_json)
    for key, value in json.loads(budget.to_json()).items():
        assert recorded[key] == value, key


def test_exhaustion_resumes_with_nothing_lost_and_nothing_double_processed(env):
    """§4: exhaustion is a safe stop and the queue is the resume state. Across
    successive runs under a tight extraction cap, every gate survivor is
    extracted exactly once (5 postings -> exactly 5 extraction calls, ever),
    none is skipped, and previously judged rows are not re-fed to the model."""
    conn, _storage, source, transport, _adapters, model = env
    transport.board = gh_board(*[gh_job(i) for i in range(1, 6)])
    tight = Budget(max_extraction_calls=2)

    run1 = run_once(env, budget=tight)
    assert run1.status == "budget_exhausted"
    assert run1.exhausted_stage == "extraction"
    assert json.loads(run1.spend_json)["extraction"] == 2
    queue = SqlitePromotionQueueRepository(conn)
    states = [r.state for r in queue.list_rows()]
    assert len(states) == 5  # nothing lost
    assert states.count("pending_extraction") == 3  # the resume state

    run_once(env, budget=tight)   # 2 more
    run_once(env, budget=tight)   # the last one
    final = run_once(env, budget=tight)  # nothing left to extract

    assert len(model.extraction_prompts) == 5  # exactly once each, ever
    rows = queue.list_rows()
    assert len(rows) == 5
    assert all(r.state == "judged" for r in rows)
    assert json.loads(final.spend_json)["extraction"] == 0
    for opp in SqliteOpportunityRepository(conn).list_for_source(source.id):
        proposals = json.loads(opp.requirement_proposals_json)
        assert [r["phrase"] for r in proposals["requirements"]] == \
            ["Python", "SQL"]


def test_closure_supersedes_pending_queue_rows_and_later_runs_never_work_them(env):
    """§4: on a closure, unfinished rows for the opportunity are cancelled
    (state superseded), and a later run with budget to spare must not spend a
    model call on them."""
    conn, _storage, source, transport, _adapters, model = env
    transport.board = gh_board(gh_job(1), gh_job(2), gh_job(3))
    frozen = Budget(max_extraction_calls=0)  # gate + enqueue, no model work
    run_once(env, budget=frozen)
    queue = SqlitePromotionQueueRepository(conn)
    assert all(r.state == "pending_extraction" for r in queue.list_rows())

    transport.board = gh_board()  # all three vanish (< guard minimum of 10)
    run_once(env, budget=frozen)  # streak 1
    run_once(env, budget=frozen)  # streak 2: closed
    repo = SqliteOpportunityRepository(conn)
    assert all(o.availability == "closed" for o in repo.list_for_source(source.id))
    rows = queue.list_rows()
    assert all(r.state == "superseded" for r in rows)
    assert all(r.superseded_reason for r in rows)  # the reason is recorded

    generous = run_once(env)  # full default budget available
    assert model.extraction_prompts == [] and model.judgment_prompts == []
    assert json.loads(generous.spend_json)["extraction"] == 0
    assert all(r.state == "superseded" for r in queue.list_rows())


def test_backlog_promotion_discards_a_closed_observation_with_its_reason(env):
    """§4: promotion out of observed-ungated selects the current open version
    at gate time; a closed observation is discarded from the backlog at that
    moment with its reason recorded, never gated and never silently dropped."""
    conn, _storage, source, transport, _adapters, _model = env
    transport.board = gh_board(gh_job(1), gh_job(2), gh_job(3))
    ungated = Budget(max_new_opportunities_gated=0)
    run1 = run_once(env, budget=ungated)
    assert run1.exhausted_stage == "gate"
    repo = SqliteOpportunityRepository(conn)
    assert all(o.backlog_state == "pending" for o in repo.list_for_source(source.id))

    transport.board = gh_board(gh_job(1), gh_job(2))  # job 3 vanishes
    run_once(env, budget=ungated)
    run_once(env, budget=ungated)  # job 3 closes while still ungated
    assert opportunity_by_external_id(conn, source.id, "3").availability == "closed"

    run_once(env)  # full budget: the backlog is finally gated
    closed = opportunity_by_external_id(conn, source.id, "3")
    assert closed.backlog_state == "discarded"
    assert closed.backlog_discard_reason  # the reason is recorded
    assert "closed" in closed.backlog_discard_reason
    assert closed.latest_gate_verdict_id is None  # never gated
    for external_id in ("1", "2"):
        assert opportunity_by_external_id(
            conn, source.id, external_id).backlog_state == "gated"


# ----------------------------------------------------------- collision (§3)

def test_id_collision_rejects_the_snapshot_committing_nothing_and_closing_nothing(env):
    """§3 collision policy: one external id carrying materially different
    payloads degrades and rejects the snapshot (commits nothing). The rejected
    poll must leave no snapshot row, no absence progress on the posting the
    collided feed omitted, and count as a failure for source health."""
    conn, storage, source, transport, _adapters, _model = env
    transport.board = gh_board(gh_job(1), gh_job(2))
    run_once(env)
    snapshots = SqliteSnapshotRepository(conn)
    assert len(snapshots.list_for_source(source.id)) == 1

    collided_a = gh_job(1, title="Engineer")
    collided_b = gh_job(1, title="Director")  # same id, different material payload
    transport.board = gh_board(collided_a, collided_b)  # job 2 also missing
    run = run_once(env)

    assert len(snapshots.list_for_source(source.id)) == 1  # nothing committed
    opp = opportunity_by_external_id(conn, source.id, "2")
    assert opp.availability == "open"  # a rejected snapshot closes nothing
    assert opp.absence_streak == 0
    refreshed = SqliteSourceRegistryRepository(conn).get(source.id)
    assert refreshed.consecutive_failures == 1
    outcomes = json.loads(run.source_outcomes_json)["sources"][source.id]
    assert outcomes["poll"] == "degraded"  # auditable, never a silent partial
