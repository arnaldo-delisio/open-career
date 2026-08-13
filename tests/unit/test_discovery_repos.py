"""Discovery storage (migration 0006): registry provenance rules, immutable
per-source-sequenced snapshots, closure plan application, versioning as the
closure mechanism, the observed-ungated backlog, the version-pinned promotion
queue lifecycle, and the dependency epoch."""

import json
import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.sqlite_discovery import (
    SqliteDependencyEpochRepository,
    SqliteDiscoveryRunRepository,
    SqliteSnapshotRepository,
    SqliteSourceRegistryRepository,
)
from adapters.storage.sqlite_opportunities import (
    SqliteOpportunityRepository,
    SqlitePromotionQueueRepository,
)
from domain.budget import Budget
from domain.closure import plan_snapshot
from domain.discovery import (
    Opportunity,
    OpportunityVersion,
    Source,
    SourceSupersession,
    StoredGateVerdict,
    apply_support_for,
    material_fingerprint,
)
from domain.ids import new_id


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA foreign_keys = ON")
    yield connection
    connection.close()


@pytest.fixture
def repos(conn):
    return {
        "sources": SqliteSourceRegistryRepository(conn),
        "snapshots": SqliteSnapshotRepository(conn),
        "opportunities": SqliteOpportunityRepository(conn),
        "queue": SqlitePromotionQueueRepository(conn),
        "runs": SqliteDiscoveryRunRepository(conn),
        "epoch": SqliteDependencyEpochRepository(conn),
    }


def add_source(repos, ats_type="greenhouse", origin="curated") -> Source:
    source = Source(id=new_id("src"), ats_type=ats_type,
                    tenant_slug=f"acme-{new_id('t')[-6:]}", origin=origin)
    repos["sources"].add(source)
    return source


def add_snapshot(repos, source, token=None):
    return repos["snapshots"].commit(
        source.id, raw_locator=f"raw/{new_id('x')}.json", content_hash="abc123",
        completion_json=json.dumps({"pages": 1, "cursor_terminal": True}),
        posting_count=1, remote_version_token=token)


def add_opportunity(repos, source, external_id="job-1", first_seen="2026-08-10T00:00:00Z"):
    opportunity = Opportunity(
        id=new_id("opp"), source_id=source.id, external_job_id=external_id,
        first_seen=first_seen, last_seen=first_seen,
        apply_support=apply_support_for(source.ats_type))
    repos["opportunities"].add(opportunity)
    return opportunity


def add_version(repos, opportunity, snapshot, version=1, title="Engineer"):
    fields = {"title": title}
    row = OpportunityVersion(
        id=new_id("opv"), opportunity_id=opportunity.id, version=version,
        snapshot_id=snapshot.id, fingerprint=material_fingerprint(fields), title=title)
    repos["opportunities"].add_version(row)
    return row


# --- registry --------------------------------------------------------------------

def test_source_round_trip_and_status(repos):
    source = add_source(repos)
    assert repos["sources"].get(source.id).status == "candidate"
    repos["sources"].set_status(source.id, "enabled")
    assert repos["sources"].get(source.id).status == "enabled"
    with pytest.raises(ValueError):
        repos["sources"].set_status(source.id, "deleted")


def test_reviewed_metadata_carries_provenance_and_a_closed_field_set(repos):
    source = add_source(repos)
    repos["sources"].set_reviewed_metadata(source.id, "industry", "fintech", "cli_edit")
    stored = repos["sources"].get(source.id)
    assert (stored.industry, stored.industry_origin) == ("fintech", "cli_edit")
    with pytest.raises(ValueError, match="not a reviewed metadata field"):
        repos["sources"].set_reviewed_metadata(source.id, "tenant_slug", "x", "cli_edit")
    with pytest.raises(ValueError, match="unknown metadata origin"):
        repos["sources"].set_reviewed_metadata(source.id, "industry", "x", "classifier")


def test_clearing_reviewed_metadata_clears_its_provenance(repos):
    source = add_source(repos)
    repos["sources"].set_reviewed_metadata(source.id, "industry", "fintech", "curated")
    repos["sources"].set_reviewed_metadata(source.id, "industry", None, "cli_edit")
    stored = repos["sources"].get(source.id)
    assert (stored.industry, stored.industry_origin) == (None, None)


def test_supersession_record_round_trips(repos):
    old, new = add_source(repos, ats_type="lever"), add_source(repos, ats_type="ashby")
    repos["sources"].record_supersession(SourceSupersession(
        id=new_id("sup"), old_source_id=old.id, new_source_id=new.id,
        origin="migration", reviewed_at="2026-08-12T00:00:00Z"))
    stored = repos["sources"].list_supersessions()
    assert len(stored) == 1
    assert (stored[0].old_source_id, stored[0].new_source_id) == (old.id, new.id)


def test_apply_support_follows_the_fill_adapter_set():
    assert apply_support_for("greenhouse") == "extension"
    assert apply_support_for("workable") == "none"
    assert apply_support_for("smartrecruiters") == "none"


# --- snapshots ---------------------------------------------------------------------

def test_snapshot_sequence_is_per_source_and_monotonic(repos):
    source_a, source_b = add_source(repos), add_source(repos)
    assert add_snapshot(repos, source_a).seq == 1
    assert add_snapshot(repos, source_a).seq == 2
    assert add_snapshot(repos, source_b).seq == 1
    assert repos["snapshots"].latest_for_source(source_a.id).seq == 2
    assert [s.seq for s in repos["snapshots"].list_for_source(source_a.id)] == [1, 2]


# --- closure plan application ---------------------------------------------------------

def states_for(repos, source):
    from domain.closure import OpportunityState
    return [OpportunityState(o.id, o.availability, o.absence_streak,
                             o.last_absence_snapshot_id)
            for o in repos["opportunities"].list_for_source(source.id)]


def test_two_absences_close_with_both_confirming_snapshot_ids(repos):
    source = add_source(repos)
    opportunity = add_opportunity(repos, source)
    snap2, snap3 = add_snapshot(repos, source), add_snapshot(repos, source)
    plan = plan_snapshot(snap2.id, set(), states_for(repos, source))
    repos["opportunities"].apply_closure_plan(plan, "2026-08-11T00:00:00Z")
    assert repos["opportunities"].get(opportunity.id).absence_streak == 1
    plan = plan_snapshot(snap3.id, set(), states_for(repos, source))
    repos["opportunities"].apply_closure_plan(plan, "2026-08-12T00:00:00Z")
    stored = repos["opportunities"].get(opportunity.id)
    assert stored.availability == "closed"
    assert json.loads(stored.closing_snapshot_ids_json) == [snap2.id, snap3.id]


def test_reappearance_resets_the_streak_and_reopens_after_closure(repos):
    source = add_source(repos)
    opportunity = add_opportunity(repos, source)
    for _ in range(2):
        snap = add_snapshot(repos, source)
        repos["opportunities"].apply_closure_plan(
            plan_snapshot(snap.id, set(), states_for(repos, source)), "2026-08-11T00:00:00Z")
    assert repos["opportunities"].get(opportunity.id).availability == "closed"
    snap = add_snapshot(repos, source)
    repos["opportunities"].apply_closure_plan(
        plan_snapshot(snap.id, {opportunity.id}, states_for(repos, source)),
        "2026-08-12T00:00:00Z")
    stored = repos["opportunities"].get(opportunity.id)
    assert (stored.availability, stored.absence_streak) == ("reopened", 0)


def test_mass_closure_cohort_closes_after_exactly_one_confirming_poll(repos):
    source = add_source(repos)
    members = [add_opportunity(repos, source, external_id=f"job-{i}") for i in range(12)]
    trigger = add_snapshot(repos, source)
    plan = plan_snapshot(trigger.id, set(), states_for(repos, source),
                         pending_cohort=repos["opportunities"].pending_cohort_for_source(source.id))
    repos["opportunities"].apply_closure_plan(plan, "2026-08-11T00:00:00Z")
    # Nothing closed yet; the cohort persists keyed to the trigger snapshot.
    assert all(repos["opportunities"].get(m.id).availability == "open" for m in members)
    cohort = repos["opportunities"].pending_cohort_for_source(source.id)
    assert cohort.triggering_snapshot_id == trigger.id
    assert cohort.pending_member_ids == frozenset(m.id for m in members)
    # The immediately next snapshot: one member reappears, the rest close.
    confirm = add_snapshot(repos, source)
    plan = plan_snapshot(confirm.id, {members[0].id}, states_for(repos, source),
                         pending_cohort=cohort)
    repos["opportunities"].apply_closure_plan(plan, "2026-08-12T00:00:00Z")
    survivor = repos["opportunities"].get(members[0].id)
    assert (survivor.availability, survivor.absence_streak) == ("open", 0)
    for member in members[1:]:
        stored = repos["opportunities"].get(member.id)
        assert stored.availability == "closed"
        assert json.loads(stored.closing_snapshot_ids_json) == [trigger.id, confirm.id]
    assert repos["opportunities"].pending_cohort_for_source(source.id) is None


def test_closure_never_touches_the_human_selected_action(repos):
    source = add_source(repos)
    opportunity = add_opportunity(repos, source)
    repos["opportunities"].set_human_action(opportunity.id, "pursue")
    for _ in range(2):
        snap = add_snapshot(repos, source)
        repos["opportunities"].apply_closure_plan(
            plan_snapshot(snap.id, set(), states_for(repos, source)), "2026-08-11T00:00:00Z")
    stored = repos["opportunities"].get(opportunity.id)
    assert stored.availability == "closed"
    assert stored.human_action == "pursue"  # the conflict surfaces, nothing deleted


# --- versions and fingerprints ---------------------------------------------------------

def test_versions_append_and_move_the_current_pointer(repos):
    source = add_source(repos)
    opportunity = add_opportunity(repos, source)
    snap = add_snapshot(repos, source)
    v1 = add_version(repos, opportunity, snap, version=1, title="Engineer")
    v2 = add_version(repos, opportunity, snap, version=2, title="Senior Engineer")
    assert repos["opportunities"].get(opportunity.id).current_version_id == v2.id
    assert [v.id for v in repos["opportunities"].list_versions(opportunity.id)] == [v1.id, v2.id]


def test_fingerprint_is_stable_and_rejects_non_material_fields():
    assert material_fingerprint({"title": "X"}) == material_fingerprint({"title": "X"})
    assert material_fingerprint({"title": "X"}) != material_fingerprint({"title": "Y"})
    with pytest.raises(ValueError, match="non-material"):
        material_fingerprint({"title": "X", "views": 9})


# --- gate verdicts ------------------------------------------------------------------------

def test_gate_verdict_is_stored_auditable_and_referenced(repos):
    source = add_source(repos)
    opportunity = add_opportunity(repos, source)
    snap = add_snapshot(repos, source)
    version = add_version(repos, opportunity, snap)
    verdict = StoredGateVerdict(
        id=new_id("gtv"), opportunity_id=opportunity.id, version_id=version.id,
        epoch=0, verdict="fail",
        dimensions_json=json.dumps([{"dimension": "seniority", "verdict": "fail",
                                     "reason": "posting band outside every family"}]))
    repos["opportunities"].record_gate_verdict(verdict)
    stored = repos["opportunities"].get(opportunity.id)
    assert stored.latest_gate_verdict_id == verdict.id
    loaded = repos["opportunities"].get_gate_verdict(verdict.id)
    assert loaded.verdict == "fail"
    assert json.loads(loaded.dimensions_json)[0]["reason"]


# --- backlog promotion -----------------------------------------------------------------

def test_backlog_promotion_returns_the_current_open_version(repos):
    source = add_source(repos)
    opportunity = add_opportunity(repos, source)
    snap = add_snapshot(repos, source)
    version = add_version(repos, opportunity, snap)
    promoted = repos["opportunities"].promote_from_backlog(opportunity.id)
    assert promoted.id == version.id
    assert repos["opportunities"].get(opportunity.id).backlog_state == "gated"


def test_backlog_promotion_discards_a_closed_observation_with_reason(repos):
    source = add_source(repos)
    opportunity = add_opportunity(repos, source)
    snap = add_snapshot(repos, source)
    add_version(repos, opportunity, snap)
    for _ in range(2):
        s = add_snapshot(repos, source)
        repos["opportunities"].apply_closure_plan(
            plan_snapshot(s.id, set(), states_for(repos, source)), "2026-08-11T00:00:00Z")
    assert repos["opportunities"].promote_from_backlog(opportunity.id) is None
    stored = repos["opportunities"].get(opportunity.id)
    assert stored.backlog_state == "discarded"
    assert stored.backlog_discard_reason == "closed before gating"


def test_backlog_promotion_is_single_shot(repos):
    source = add_source(repos)
    opportunity = add_opportunity(repos, source)
    snap = add_snapshot(repos, source)
    add_version(repos, opportunity, snap)
    repos["opportunities"].promote_from_backlog(opportunity.id)
    with pytest.raises(ValueError, match="not in the backlog"):
        repos["opportunities"].promote_from_backlog(opportunity.id)


# --- promotion queue lifecycle ---------------------------------------------------------

def queue_setup(repos):
    source = add_source(repos)
    opportunity = add_opportunity(repos, source)
    snap = add_snapshot(repos, source)
    version = add_version(repos, opportunity, snap)
    row = repos["queue"].enqueue(opportunity.id, version.id, lane_rank=0,
                                 first_seen=opportunity.first_seen, epoch=0)
    return source, opportunity, snap, version, row


def test_enqueue_merges_by_opportunity_version_key(repos):
    _, opportunity, _, version, row = queue_setup(repos)
    again = repos["queue"].enqueue(opportunity.id, version.id, lane_rank=0,
                                   first_seen=opportunity.first_seen, epoch=0)
    assert again.id == row.id and again.enqueue_seq == row.enqueue_seq


def test_stage_transitions_walk_the_table_and_reject_the_rest(repos):
    *_, row = queue_setup(repos)
    repos["queue"].transition(row.id, "extracted", coverage_bp=7500)
    repos["queue"].transition(row.id, "pending_judgment")
    repos["queue"].transition(row.id, "judged")
    assert repos["queue"].get(row.id).state == "judged"
    with pytest.raises(ValueError, match="illegal queue transition"):
        repos["queue"].transition(row.id, "extracted")


def test_extracted_transition_requires_coverage(repos):
    *_, row = queue_setup(repos)
    with pytest.raises(ValueError, match="coverage_bp"):
        repos["queue"].transition(row.id, "extracted")


def test_claim_supersedes_a_row_whose_version_is_no_longer_current(repos):
    _, opportunity, snap, _, row = queue_setup(repos)
    add_version(repos, opportunity, snap, version=2, title="Changed")
    assert repos["queue"].claim(row.id, current_epoch=0) is None
    stored = repos["queue"].get(row.id)
    assert stored.state == "superseded" and "no longer the current" in stored.superseded_reason


def test_claim_supersedes_a_row_from_an_older_epoch(repos):
    *_, row = queue_setup(repos)
    assert repos["queue"].claim(row.id, current_epoch=3) is None
    assert repos["queue"].get(row.id).state == "superseded"


def test_claim_returns_a_current_row(repos):
    *_, row = queue_setup(repos)
    assert repos["queue"].claim(row.id, current_epoch=0).id == row.id


def test_closure_supersedes_all_unfinished_rows(repos):
    source, opportunity, _, _, row = queue_setup(repos)
    for _ in range(2):
        snap = add_snapshot(repos, source)
        repos["opportunities"].apply_closure_plan(
            plan_snapshot(snap.id, set(), states_for(repos, source)), "2026-08-11T00:00:00Z")
    repos["queue"].supersede_for_opportunity(opportunity.id, None, "opportunity closed")
    assert repos["queue"].get(row.id).state == "superseded"


def test_epoch_bump_supersedes_stale_rows_in_bulk(repos):
    *_, row = queue_setup(repos)
    current = repos["epoch"].bump()
    repos["queue"].supersede_stale_epochs(current, "dependency epoch advanced")
    assert repos["queue"].get(row.id).state == "superseded"


def test_bounded_retry_then_terminal_failed_then_explicit_cli_retry(repos):
    *_, row = queue_setup(repos)
    repos["queue"].release_failed(row.id, "model timeout", current_run_seq=1)
    stored = repos["queue"].get(row.id)
    assert (stored.state, stored.attempts, stored.next_attempt_run_seq) == (
        "pending_extraction", 1, 2)
    # Backoff respected: not claimable before its next-attempt run.
    assert repos["queue"].pending_for_stage("pending_extraction", current_run_seq=1) == []
    assert [r.id for r in repos["queue"].pending_for_stage(
        "pending_extraction", current_run_seq=2)] == [row.id]
    repos["queue"].release_failed(row.id, "model timeout", current_run_seq=2)
    repos["queue"].release_failed(row.id, "model timeout", current_run_seq=4)
    stored = repos["queue"].get(row.id)
    assert (stored.state, stored.failure_reason) == ("failed", "model timeout")
    # Terminal failed is excluded from claiming; only explicit CLI retry revives.
    assert repos["queue"].claim(row.id, current_epoch=0) is None
    repos["queue"].retry_failed(row.id, current_run_seq=5)
    stored = repos["queue"].get(row.id)
    assert (stored.state, stored.attempts, stored.failure_reason) == (
        "pending_extraction", 0, None)


# --- runs and the epoch counter -----------------------------------------------------------

def test_run_record_locks_the_budget_and_records_the_outcome(repos):
    budget = Budget()
    run = repos["runs"].start(budget.to_json(), epoch=repos["epoch"].current())
    assert run.status == "running" and run.run_seq == 1
    assert json.loads(run.budget_json)["max_fetches"] == 2000
    repos["runs"].finish(run.id, "budget_exhausted",
                         spend_json=json.dumps({"fetch": 2000}),
                         source_outcomes_json=json.dumps({}),
                         exhausted_stage="fetch")
    stored = repos["runs"].get(run.id)
    assert (stored.status, stored.exhausted_stage) == ("budget_exhausted", "fetch")
    assert stored.finished_at is not None
    assert repos["runs"].start(budget.to_json(), epoch=0).run_seq == 2


def test_unknown_terminal_run_status_is_rejected(repos):
    run = repos["runs"].start(Budget().to_json(), epoch=0)
    with pytest.raises(ValueError):
        repos["runs"].finish(run.id, "done", "{}", "{}")


def test_dependency_epoch_starts_at_zero_and_bumps(repos):
    assert repos["epoch"].current() == 0
    assert repos["epoch"].bump() == 1
    assert repos["epoch"].bump() == 2
