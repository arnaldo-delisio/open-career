"""Gauntlet repository semantics (spec: the scope's
decisions/gauntlet-design.md, "Storage", "Attempts and the effective run",
"Admission is atomic", "The approval gate"): append-only runs, the partial
unique index backstop, reservation fencing (expired-claim race, stale
consume), effective-run resolution by seq, and every approval path."""

import json
import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.sqlite_packages import SqlitePackageRepository
from domain.gauntlet import SUITE_VERSION, ReservationLostError
from domain.ids import new_id
from domain.packages import APPROVED, PackageStateError


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.execute(
            "INSERT INTO role_families (id, name, rationale) VALUES ('rf_1', 'FDE', 'r')")
    yield conn
    conn.close()


@pytest.fixture
def repo(conn):
    return SqlitePackageRepository(conn)


def _bundle():
    return dict(content_model_json="{}",
                context_snapshot_locator="l", input_context_hash="h",
                verifier_report_json="{}", ats_report_json="{}",
                artifact_locator="l2", artifact_hash="h2")


@pytest.fixture
def verified(repo):
    package = repo.get_or_create_base_package("rf_1")
    v = repo.reserve_version(package.id, "owner-a", 60)
    repo.finalize_verified(v.id, "owner-a", 1, **_bundle())
    return repo.get_version(v.id)


def _run_fields(verdict="PASS", complete=1):
    return dict(run_id=new_id("grun"), complete=complete,
                report_json=json.dumps({"verdict": verdict, "stop_reason": "t"}),
                prompt_inputs_locator="g/p", prompt_inputs_hash="ph",
                raw_completions_locator="g/c", raw_completions_hash="ch",
                resolved_models_json="{}", policy_snapshot_locator="g/s",
                policy_snapshot_hash="sh")


def _adjudicate(repo, version_id, verdict="PASS", complete=1,
                suite=SUITE_VERSION, owner=None):
    owner = owner or new_id("judge")
    assert repo.claim_gauntlet_reservation(version_id, suite, owner, 60)
    return repo.insert_gauntlet_run(version_id, suite, owner,
                                    **_run_fields(verdict, complete))


# -- append-only runs and the partial index -----------------------------------

def test_run_insert_allocates_seq_and_attempt(repo, verified):
    run = _adjudicate(repo, verified.id, complete=0)
    assert run.attempt == 1 and run.complete == 0
    run2 = _adjudicate(repo, verified.id, complete=1)
    assert run2.attempt == 2 and run2.seq > run.seq


def test_partial_index_blocks_a_second_complete_same_suite_row(conn, repo, verified):
    _adjudicate(repo, verified.id, complete=1)
    # Application logic bypassed on purpose: the database itself must refuse.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gauntlet_runs (id, package_version_id, suite_version,"
            " attempt, complete, report_json, prompt_inputs_locator,"
            " prompt_inputs_hash, raw_completions_locator, raw_completions_hash,"
            " resolved_models_json, policy_snapshot_locator, policy_snapshot_hash)"
            " VALUES ('grun_x', ?, ?, 9, 1, '{}', 'l', 'h', 'l', 'h', '{}', 'l', 'h')",
            (verified.id, SUITE_VERSION))


def test_reclaim_after_complete_attempt_is_refused_at_the_claim(repo, verified):
    _adjudicate(repo, verified.id, complete=1)
    with pytest.raises(PackageStateError, match="complete Gauntlet attempt"):
        repo.claim_gauntlet_reservation(verified.id, SUITE_VERSION, "o2", 60)


def test_incomplete_attempt_leaves_the_suite_rerunnable(repo, verified):
    _adjudicate(repo, verified.id, complete=0)
    run = _adjudicate(repo, verified.id, verdict="FAIL", complete=1)
    assert run.attempt == 2


def test_a_newer_suite_is_the_only_other_path_to_rejudging(repo, verified):
    _adjudicate(repo, verified.id, complete=1)
    run = _adjudicate(repo, verified.id, suite="gauntlet-next")
    assert run.suite_version == "gauntlet-next" and run.attempt == 1


def test_effective_run_is_complete_with_greatest_seq(repo, verified):
    _adjudicate(repo, verified.id, verdict="FAIL", complete=0)
    kept = _adjudicate(repo, verified.id, verdict="ATTENTION", complete=1)
    _adjudicate(repo, verified.id, suite="gauntlet-next", complete=0)
    effective = repo.effective_gauntlet_run(verified.id, SUITE_VERSION)
    assert effective.id == kept.id and effective.verdict == "ATTENTION"
    assert repo.effective_gauntlet_run(verified.id, "gauntlet-next") is None
    runs = repo.list_gauntlet_runs(verified.id)
    assert [r.seq for r in runs] == sorted((r.seq for r in runs), reverse=True)


# -- reservation fencing ------------------------------------------------------

def test_concurrent_claim_loses_while_reservation_is_live(repo, verified):
    assert repo.claim_gauntlet_reservation(verified.id, SUITE_VERSION, "o1", 60)
    assert not repo.claim_gauntlet_reservation(verified.id, SUITE_VERSION, "o2", 60)


def test_expired_reservation_is_taken_over_and_stale_consume_discards(repo, verified):
    assert repo.claim_gauntlet_reservation(verified.id, SUITE_VERSION, "dead", -5)
    # The expired-claim race: a successor takes over past expiry, minting its
    # own owner token.
    assert repo.claim_gauntlet_reservation(verified.id, SUITE_VERSION, "live", 60)
    # The stale worker's consume matches zero rows and inserts NOTHING.
    with pytest.raises(ReservationLostError):
        repo.insert_gauntlet_run(verified.id, SUITE_VERSION, "dead", **_run_fields())
    assert repo.list_gauntlet_runs(verified.id) == []
    # The live worker completes normally.
    run = repo.insert_gauntlet_run(verified.id, SUITE_VERSION, "live", **_run_fields())
    assert run.attempt == 1


def test_expired_own_reservation_cannot_consume(repo, verified):
    assert repo.claim_gauntlet_reservation(verified.id, SUITE_VERSION, "slow", -5)
    with pytest.raises(ReservationLostError):
        repo.insert_gauntlet_run(verified.id, SUITE_VERSION, "slow", **_run_fields())


def test_renewal_is_fenced_by_owner_and_expiry(repo, verified):
    assert repo.claim_gauntlet_reservation(verified.id, SUITE_VERSION, "o1", 60)
    assert repo.renew_gauntlet_reservation(verified.id, SUITE_VERSION, "o1", 60)
    assert not repo.renew_gauntlet_reservation(verified.id, SUITE_VERSION, "o2", 60)
    # A live reservation is untouchable by another claimant, whatever ttl the
    # claimant asks for.
    assert not repo.claim_gauntlet_reservation(verified.id, SUITE_VERSION, "e1", -5)


def test_expired_reservation_renewal_permanently_stops_the_worker(repo, verified):
    assert repo.claim_gauntlet_reservation(verified.id, SUITE_VERSION, "w", -5)
    assert not repo.renew_gauntlet_reservation(verified.id, SUITE_VERSION, "w", 60)


# -- the approval gate --------------------------------------------------------

def test_approval_without_any_run_is_refused_for_every_caller(repo, verified):
    with pytest.raises(PackageStateError, match="no effective Gauntlet run"):
        repo.approve(verified.id, "2026-08-13T00:00:00Z")
    # An override reason cannot bypass judging after a crash or outage.
    with pytest.raises(PackageStateError, match="no effective Gauntlet run"):
        repo.approve(verified.id, "2026-08-13T00:00:00Z", override=True,
                     override_reason="I am sure")


def test_incomplete_run_is_not_an_effective_run_for_approval(repo, verified):
    _adjudicate(repo, verified.id, verdict="INCOMPLETE", complete=0)
    with pytest.raises(PackageStateError, match="no effective Gauntlet run"):
        repo.approve(verified.id, "2026-08-13T00:00:00Z")


def test_pass_verdict_approves_and_records_the_decision(repo, verified):
    run = _adjudicate(repo, verified.id, verdict="PASS")
    repo.approve(verified.id, "2026-08-13T00:00:00Z")
    assert repo.get_version(verified.id).status == APPROVED
    (decision,) = repo.list_approval_decisions(verified.id)
    assert decision[2] == run.id and decision[3] == "PASS" and decision[4] == 0


def test_fail_verdict_blocks_without_an_override(repo, verified):
    _adjudicate(repo, verified.id, verdict="FAIL")
    with pytest.raises(PackageStateError, match="verdict is FAIL"):
        repo.approve(verified.id, "2026-08-13T00:00:00Z")


def test_override_requires_a_nonempty_reason_and_records_it(repo, verified):
    run = _adjudicate(repo, verified.id, verdict="FAIL")
    with pytest.raises(PackageStateError, match="non-empty reason"):
        repo.approve(verified.id, "2026-08-13T00:00:00Z", override=True,
                     override_reason="  ")
    repo.approve(verified.id, "2026-08-13T00:00:00Z", override=True,
                 override_reason="stale never_render literal, verified by hand")
    (decision,) = repo.list_approval_decisions(verified.id)
    assert decision[2] == run.id and decision[3] == "FAIL" and decision[4] == 1
    assert "verified by hand" in decision[5]


def test_override_waives_only_a_recorded_fail_or_attention(repo, verified):
    _adjudicate(repo, verified.id, verdict="PASS")
    with pytest.raises(PackageStateError, match="nothing to override"):
        repo.approve(verified.id, "2026-08-13T00:00:00Z", override=True,
                     override_reason="r")


def test_stale_suite_pass_never_approves_on_any_path(conn, verified):
    """The repository owns the current suite version: an older suite's PASS
    (or attempt) is invisible to approval, plain and override alike."""
    old = SqlitePackageRepository(conn)  # judged under the shipped suite
    _adjudicate(old, verified.id, verdict="PASS")
    newer = SqlitePackageRepository(conn,
                                    suite_version_provider=lambda: "gauntlet-99")
    with pytest.raises(PackageStateError, match="gauntlet-99"):
        newer.approve(verified.id, "2026-08-13T00:00:00Z")
    with pytest.raises(PackageStateError, match="gauntlet-99"):
        newer.approve(verified.id, "2026-08-13T00:00:00Z", override=True,
                      override_reason="r")


def test_runs_are_append_only_write_once_on_insert(repo, verified):
    with pytest.raises(PackageStateError, match="write-once"):
        repo.claim_gauntlet_reservation(verified.id, SUITE_VERSION, "o", 60)
        fields = _run_fields()
        fields.pop("report_json")
        repo.insert_gauntlet_run(verified.id, SUITE_VERSION, "o", **fields)
