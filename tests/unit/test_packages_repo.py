"""Package lifecycle seam tests (spec: decisions/package-generation-design.md,
"Storage, states, CLI"): identity, transitions, required fields, write-once,
lease fencing, recovery."""

import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.sqlite_packages import SqlitePackageRepository
from domain.packages import (
    APPROVED,
    FAILED,
    GENERATING,
    VERIFIED,
    LeaseLostError,
    PackageStateError,
    object_locator,
)

REPORT = '{"stage": "verify", "error": "test failure"}'


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.execute(
            "INSERT INTO role_families (id, name, rationale) VALUES ('rf_1', 'FDE', 'r')")
        conn.execute(
            "INSERT INTO role_families (id, name, rationale) VALUES ('rf_2', 'Platform', 'r')")
    yield conn
    conn.close()


@pytest.fixture
def repo(conn):
    return SqlitePackageRepository(conn)


def _bundle(**overrides):
    bundle = dict(
        content_model_json="{}",
        context_snapshot_locator="packages/p/v1/g1/context.json",
        input_context_hash="hash",
        verifier_report_json="{}",
        ats_report_json="{}",
        artifact_locator="packages/p/v1/g1/cv.pdf",
        artifact_hash="pdfhash",
    )
    bundle.update(overrides)
    return bundle


def _reserve(repo, lease_seconds=60):
    package = repo.get_or_create_base_package("rf_1")
    return repo.reserve_version(package.id, "owner-a", lease_seconds)


def test_one_base_package_per_family(repo, conn):
    first = repo.get_or_create_base_package("rf_1")
    assert repo.get_or_create_base_package("rf_1").id == first.id
    assert repo.get_or_create_base_package("rf_2").id != first.id
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO packages (id, role_family_id) VALUES ('pkg_dup', 'rf_1')")


def test_package_requires_existing_family(repo):
    with pytest.raises(sqlite3.IntegrityError):
        repo.get_or_create_base_package("rf_missing")


def test_reserve_appends_versions(repo):
    package = repo.get_or_create_base_package("rf_1")
    v1 = repo.reserve_version(package.id, "owner-a", 60)
    v2 = repo.reserve_version(package.id, "owner-b", 60)
    assert (v1.version, v2.version) == (1, 2)
    assert v1.status == GENERATING
    assert v1.lease_owner == "owner-a" and v1.lease_generation == 1
    assert v1.lease_expires_at is not None


def test_record_progress_and_finalize_verified(repo):
    v = _reserve(repo)
    repo.record_progress(v.id, "owner-a", 1, content_model_json="{}")
    repo.finalize_verified(v.id, "owner-a", 1, **_bundle())
    final = repo.get_version(v.id)
    assert final.status == VERIFIED
    assert final.artifact_hash == "pdfhash"


def test_finalize_requires_full_bundle(repo):
    v = _reserve(repo)
    with pytest.raises(PackageStateError, match="full audit bundle"):
        repo.finalize_verified(v.id, "owner-a", 1, **_bundle(artifact_hash=None))
    assert repo.get_version(v.id).status == GENERATING


def test_finalized_bundle_fields_are_write_once(repo):
    v = _reserve(repo)
    repo.finalize_verified(v.id, "owner-a", 1, **_bundle())
    with pytest.raises(PackageStateError, match="write-once"):
        repo.record_progress(v.id, "owner-a", 1, content_model_json='{"changed": 1}')
    with pytest.raises(PackageStateError):
        repo.finalize_verified(v.id, "owner-a", 1, **_bundle(artifact_hash="other"))
    assert repo.get_version(v.id).artifact_hash == "pdfhash"


def test_fail_requires_structured_report_and_keeps_partials(repo):
    v = _reserve(repo)
    repo.record_progress(v.id, "owner-a", 1,
                         context_snapshot_locator="loc", input_context_hash="h")
    with pytest.raises(PackageStateError, match="failure report"):
        repo.fail(v.id, "owner-a", 1, "  ")
    repo.fail(v.id, "owner-a", 1, REPORT)
    failed = repo.get_version(v.id)
    assert failed.status == FAILED
    assert failed.failure_report_json == REPORT
    assert failed.context_snapshot_locator == "loc"  # partial artifacts retained


def test_early_failure_keeps_snapshot_fields_null(repo):
    v = _reserve(repo)
    repo.fail(v.id, "owner-a", 1, REPORT)
    failed = repo.get_version(v.id)
    assert failed.context_snapshot_locator is None
    assert failed.input_context_hash is None


def test_approve_sets_pointer_in_same_operation(repo):
    v = _reserve(repo)
    repo.finalize_verified(v.id, "owner-a", 1, **_bundle())
    repo.approve(v.id, "2026-08-11T00:00:00Z")
    approved = repo.get_version(v.id)
    assert approved.status == APPROVED and approved.approved_at is not None
    assert repo.get_package(v.package_id).approved_version_id == v.id


def test_approve_rejects_non_verified(repo):
    v = _reserve(repo)
    with pytest.raises(PackageStateError, match="VERIFIED"):
        repo.approve(v.id, "2026-08-11T00:00:00Z")
    repo.fail(v.id, "owner-a", 1, REPORT)
    with pytest.raises(PackageStateError):
        repo.approve(v.id, "2026-08-11T00:00:00Z")
    assert repo.get_package(v.package_id).approved_version_id is None


def test_later_failed_generation_never_displaces_approved(repo):
    package = repo.get_or_create_base_package("rf_1")
    v1 = repo.reserve_version(package.id, "owner-a", 60)
    repo.finalize_verified(v1.id, "owner-a", 1, **_bundle())
    repo.approve(v1.id, "2026-08-11T00:00:00Z")
    v2 = repo.reserve_version(package.id, "owner-a", 60)
    repo.fail(v2.id, "owner-a", 1, REPORT)
    assert repo.get_package(package.id).approved_version_id == v1.id


# -- lease -----------------------------------------------------------------

def test_wrong_owner_or_generation_cannot_write(repo):
    v = _reserve(repo)
    with pytest.raises(LeaseLostError):
        repo.record_progress(v.id, "owner-b", 1, content_model_json="{}")
    with pytest.raises(LeaseLostError):
        repo.fail(v.id, "owner-a", 2, REPORT)
    assert repo.get_version(v.id).status == GENERATING


def test_expired_lease_stops_worker_and_renewal_cannot_resurrect(repo):
    v = _reserve(repo, lease_seconds=-5)
    assert repo.check_lease(v.id, "owner-a", 1) is False
    assert repo.renew_lease(v.id, "owner-a", 1, 60) is False  # zero-row renewal
    with pytest.raises(LeaseLostError):
        repo.finalize_verified(v.id, "owner-a", 1, **_bundle())


def test_live_lease_renews_and_holds(repo):
    v = _reserve(repo, lease_seconds=60)
    assert repo.check_lease(v.id, "owner-a", 1) is True
    assert repo.renew_lease(v.id, "owner-a", 1, 120) is True


def test_recovery_claims_only_expired_leases(repo):
    live = _reserve(repo, lease_seconds=60)
    assert repo.claim_expired_and_fail(live.id, REPORT) is False  # never from under a live owner
    assert repo.get_version(live.id).status == GENERATING

    package = repo.get_or_create_base_package("rf_2")
    stale = repo.reserve_version(package.id, "owner-a", -5)
    assert repo.claim_expired_and_fail(stale.id, REPORT) is True
    claimed = repo.get_version(stale.id)
    assert claimed.status == FAILED
    assert claimed.lease_generation == 2  # fence bumped


def test_expired_claim_races_finalization(repo):
    """An expired-lease claim racing a finalization: whoever wins the atomic
    compare-and-set wins; the loser writes nothing."""
    v = _reserve(repo, lease_seconds=-5)
    assert repo.claim_expired_and_fail(v.id, REPORT) is True
    with pytest.raises(PackageStateError):
        repo.finalize_verified(v.id, "owner-a", 1, **_bundle())
    assert repo.get_version(v.id).status == FAILED


def test_recovered_row_rejects_resuming_worker_writes(repo):
    v = _reserve(repo, lease_seconds=-5)
    assert repo.claim_expired_and_fail(v.id, REPORT) is True
    assert repo.check_lease(v.id, "owner-a", 1) is False
    with pytest.raises(PackageStateError):
        repo.record_progress(v.id, "owner-a", 1, content_model_json="{}")


def test_object_locator_embeds_generation_fence():
    assert object_locator("pkg_1", 2, 3, "context.json") == "packages/pkg_1/v2/g3/context.json"
