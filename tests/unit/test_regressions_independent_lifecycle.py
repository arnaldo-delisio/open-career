"""INDEPENDENT regression pass, lifecycle side: write-once bundle fields, the
expired-lease/finalization race, the crash matrix (crash immediately after
each object write before its row-progress update, then recovery), and the
recovered-then-resuming worker whose writes must stay inert. Spec:
decisions/package-generation-design.md, "Storage, states, CLI"."""

import json
import sqlite3

import pytest

from adapters.storage.local import LocalStorageAdapter
from adapters.storage.migrations import migrate
from adapters.storage.sqlite_packages import SqlitePackageRepository
from domain.ats_check import expected_section_tokens
from domain.generation import build_verbatim_model
from domain.packages import (
    FAILED,
    GENERATING,
    VERIFIED,
    LeaseLostError,
    PackageStateError,
    object_locator,
)
from domain.pipeline import GenerationPipeline, recover_expired
from domain.ports import CvRenderer, PdfTextExtractor, StorageObjectExistsError
from tests.unit.test_regressions_independent import make_context

BUNDLE = dict(
    content_model_json='{"cv": 1}',
    context_snapshot_locator="packages/p/v1/g1/context.json",
    input_context_hash="hash-1",
    verifier_report_json='{"passed": true}',
    ats_report_json='{"passed": true}',
    artifact_locator="packages/p/v1/g1/cv.pdf",
    artifact_hash="pdf-hash-1",
)
REPORT = '{"stage": "test", "error": "independent"}'


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "lifecycle.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.execute("INSERT INTO role_families (id, name, rationale)"
                     " VALUES ('irf_1', 'Platform', 'r')")
    yield conn
    conn.close()


@pytest.fixture
def repo(conn):
    return SqlitePackageRepository(conn)


@pytest.fixture
def storage(tmp_path):
    return LocalStorageAdapter(tmp_path / "instance")


def _expire(conn, version_id):
    with conn:
        conn.execute("UPDATE package_versions SET lease_expires_at ="
                     " '2000-01-01T00:00:00.000Z' WHERE id = ?", (version_id,))


# -- write-once finalized bundle fields --------------------------------------

def test_finalized_bundle_fields_are_write_once(repo):
    package = repo.get_or_create_base_package("irf_1")
    v = repo.reserve_version(package.id, "owner-i", 60)
    repo.finalize_verified(v.id, "owner-i", 1, **BUNDLE)
    with pytest.raises(PackageStateError):
        repo.record_progress(v.id, "owner-i", 1, content_model_json='{"cv": 2}')
    with pytest.raises(PackageStateError):
        repo.finalize_verified(v.id, "owner-i", 1,
                               **{**BUNDLE, "artifact_hash": "pdf-hash-2"})
    with pytest.raises(PackageStateError):
        repo.fail(v.id, "owner-i", 1, REPORT)
    final = repo.get_version(v.id)
    assert final.status == VERIFIED
    assert final.content_model_json == '{"cv": 1}'
    assert final.artifact_hash == "pdf-hash-1"
    assert final.failure_report_json is None


# -- expired-lease claim racing finalization ---------------------------------

def test_expired_claim_beats_late_finalization(repo):
    """Recovery wins the compare-and-set on an expired lease; the losing
    worker's finalization writes nothing."""
    package = repo.get_or_create_base_package("irf_1")
    v = repo.reserve_version(package.id, "owner-i", -5)
    assert repo.claim_expired_and_fail(v.id, REPORT) is True
    with pytest.raises(PackageStateError):
        repo.finalize_verified(v.id, "owner-i", 1, **BUNDLE)
    after = repo.get_version(v.id)
    assert after.status == FAILED
    assert after.failure_report_json == REPORT
    assert after.artifact_locator is None and after.artifact_hash is None
    assert after.lease_generation == 2  # fence bumped: old lease unresurrectable
    assert repo.renew_lease(v.id, "owner-i", 1, 60) is False


def test_finalization_beats_late_expired_claim(repo):
    """The other order: a live owner finalizes first; the claim then loses
    (and a claim never fires against a live lease at all)."""
    package = repo.get_or_create_base_package("irf_1")
    v = repo.reserve_version(package.id, "owner-i", 60)
    assert repo.claim_expired_and_fail(v.id, REPORT) is False  # live lease
    repo.finalize_verified(v.id, "owner-i", 1, **BUNDLE)
    assert repo.claim_expired_and_fail(v.id, REPORT) is False
    after = repo.get_version(v.id)
    assert after.status == VERIFIED and after.failure_report_json is None


# -- crash matrix ------------------------------------------------------------

class Crash(BaseException):
    """Simulates a process death: not an Exception, so the pipeline's
    structured-failure path cannot intercept it."""


class CrashingStorage:
    """Delegates to a real adapter, but dies immediately AFTER the named
    object write completes and BEFORE its row-progress update can run."""

    def __init__(self, inner, crash_after: str):
        self._inner = inner
        self._crash_after = crash_after

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def write_text_new(self, relative_path, content):
        self._inner.write_text_new(relative_path, content)
        if self._crash_after == "write_text_new":
            raise Crash("process died after snapshot write")

    def write_bytes_new(self, relative_path, content):
        self._inner.write_bytes_new(relative_path, content)
        if self._crash_after == "write_bytes_new":
            raise Crash("process died after pdf write")


class FakeRenderer(CvRenderer):
    def render_pdf(self, cv):
        return b"%PDF-1.4 independent"


class MatchingExtractor(PdfTextExtractor):
    """Extraction that echoes the CV's own expected token stream, with a
    configurable page count."""

    def __init__(self, cv_ref, pages=1):
        self._cv_ref = cv_ref
        self._pages = pages

    def extract_layout(self, pdf):
        tokens = [t for _k, ts in expected_section_tokens(self._cv_ref[0]) for t in ts]
        return " ".join(tokens) + "\f" * self._pages


def _pipeline_env(repo, storage, crash_after=None, pages=1):
    context = make_context()
    cv, _ = build_verbatim_model(context, "2026-08-12T00:00:00Z")
    cv_ref = [cv]
    wrapped = CrashingStorage(storage, crash_after) if crash_after else storage
    pipeline = GenerationPipeline(repo, wrapped, FakeRenderer(),
                                  MatchingExtractor(cv_ref, pages), drafter=None)
    return pipeline, context, cv


def test_crash_after_snapshot_write_before_progress_then_recovery(
        conn, repo, storage):
    package = repo.get_or_create_base_package("irf_1")
    pipeline, context, cv = _pipeline_env(repo, storage,
                                          crash_after="write_text_new")
    with pytest.raises(Crash):
        pipeline.generate(package.id, context, edited_model=cv)
    v = repo.list_versions(package.id)[0]
    snapshot_loc = object_locator(package.id, v.version, 1, "context.json")
    # Post-crash state: object on disk, row untouched by progress.
    assert v.status == GENERATING
    assert v.context_snapshot_locator is None
    assert storage.exists(snapshot_loc)
    # Recovery after the lease expires: FAILED with a structured interruption
    # report; the object is reconciled as an attributable orphan.
    _expire(conn, v.id)
    claimed, orphans = recover_expired(repo, storage, repo.list_versions(package.id))
    assert claimed == [v.id]
    assert snapshot_loc in orphans
    recovered = repo.get_version(v.id)
    assert recovered.status == FAILED
    assert "interrupted" in json.loads(recovered.failure_report_json)["error"]
    assert recovered.context_snapshot_locator is None  # never referenced


def test_crash_after_pdf_write_before_progress_then_recovery(
        conn, repo, storage):
    package = repo.get_or_create_base_package("irf_1")
    pipeline, context, cv = _pipeline_env(repo, storage,
                                          crash_after="write_bytes_new")
    with pytest.raises(Crash):
        pipeline.generate(package.id, context, edited_model=cv)
    v = repo.list_versions(package.id)[0]
    pdf_loc = object_locator(package.id, v.version, 1, "cv.pdf")
    snapshot_loc = object_locator(package.id, v.version, 1, "context.json")
    # Snapshot progress landed before the crash; artifact progress did not.
    assert v.status == GENERATING
    assert v.context_snapshot_locator == snapshot_loc
    assert v.artifact_locator is None
    assert storage.exists(pdf_loc)
    _expire(conn, v.id)
    claimed, orphans = recover_expired(repo, storage, repo.list_versions(package.id))
    assert claimed == [v.id]
    assert pdf_loc in orphans
    assert snapshot_loc not in orphans  # referenced partial, retained not orphaned
    recovered = repo.get_version(v.id)
    assert recovered.status == FAILED
    assert recovered.context_snapshot_locator == snapshot_loc
    assert recovered.artifact_locator is None


def test_resumed_worker_after_failed_row_cannot_corrupt(conn, repo, storage):
    """A recovered-then-resuming worker attempting each object write after its
    row was FAILED: row writes rejected, existing objects never overwritten,
    a stale new object lands only as an inert, attributable orphan."""
    package = repo.get_or_create_base_package("irf_1")
    v = repo.reserve_version(package.id, "owner-i", -5)
    snapshot_loc = object_locator(package.id, v.version, 1, "context.json")
    pdf_loc = object_locator(package.id, v.version, 1, "cv.pdf")
    storage.write_text_new(snapshot_loc, '{"snapshot": "original"}')
    assert repo.claim_expired_and_fail(v.id, REPORT) is True

    # Row writes: every path is rejected, nothing lands.
    assert repo.check_lease(v.id, "owner-i", 1) is False
    with pytest.raises(PackageStateError):
        repo.record_progress(v.id, "owner-i", 1,
                             context_snapshot_locator=snapshot_loc)
    with pytest.raises(PackageStateError):
        repo.finalize_verified(v.id, "owner-i", 1, **BUNDLE)
    with pytest.raises((PackageStateError, LeaseLostError)):
        repo.fail(v.id, "owner-i", 1, '{"stage": "late", "error": "stale"}')

    # Object writes: an existing object is never replaced...
    with pytest.raises(StorageObjectExistsError):
        storage.write_text_new(snapshot_loc, '{"snapshot": "corrupted"}')
    assert storage.read_text(snapshot_loc) == '{"snapshot": "original"}'
    # ...and a new stale-generation object is an inert orphan: attributable
    # by locator, listed by recovery, referenced by no row.
    storage.write_bytes_new(pdf_loc, b"%PDF stale worker output")
    _claimed, orphans = recover_expired(repo, storage, repo.list_versions(package.id))
    assert pdf_loc in orphans and snapshot_loc in orphans
    after = repo.get_version(v.id)
    assert after.status == FAILED
    assert after.failure_report_json == REPORT
    assert after.artifact_locator is None and after.artifact_hash is None


# -- two-page overflow is a hard build failure at the pipeline ----------------

def test_overflow_is_a_hard_pipeline_failure_never_shipped(repo, storage):
    package = repo.get_or_create_base_package("irf_1")
    pipeline, context, cv = _pipeline_env(repo, storage, pages=3)
    result = pipeline.generate(package.id, context, edited_model=cv)
    assert result.status == FAILED
    v = repo.get_version(result.version_id)
    assert v.status == FAILED
    failure = json.loads(v.failure_report_json)
    assert failure["stage"] == "ats-check"
    ats = json.loads(v.ats_report_json)
    assert any(f["check"] == "page-budget" for f in ats["findings"])
    with pytest.raises(PackageStateError):  # a failed version can never ship
        repo.approve(v.id, "2026-08-12T00:00:00Z")
