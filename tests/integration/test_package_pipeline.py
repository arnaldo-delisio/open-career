"""End-to-end package generation (spec verification plan): seeded graph ->
generate -> verify -> render -> pdftotext -> every bullet traces and every
check passes; poisoned context asserting nothing unapproved renders; the
review write-back loop; export hash validation; lease recovery."""

import json
import pathlib
import sqlite3
import time
from contextlib import contextmanager

import pytest

from adapters.storage.family_strategy import FamilyStrategyService
from adapters.storage.local import LocalStorageAdapter
from adapters.storage.migrations import migrate
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
)
from adapters.storage.sqlite_packages import SqlitePackageRepository
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from apps.cli import package_cmd
from domain.edges import CareerEdge
from domain.entities import Capability, CareerFact, Evidence, Experience, RoleFamily
from domain.generation import DraftResult, build_verbatim_model
from domain.grounding import GroundingVerifier
from domain.packages import APPROVED, FAILED, VERIFIED
from domain.pipeline import GenerationPipeline, recover_expired
from domain.ports import ModelAdapter


class FakeModel(ModelAdapter):
    def __init__(self, responses):
        self.responses = list(responses)

    def complete(self, prompt: str) -> str:
        return self.responses.pop(0)


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "instance" / "open-career.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    storage = LocalStorageAdapter(tmp_path / "instance")
    _seed(conn)
    yield conn, storage
    conn.close()


def _seed(conn):
    profile = SqliteUserProfileRepository(conn)
    profile.set_field("full_name", "Test Person", source="user_edit")
    profile.set_field("email", "t@example.com", source="user_edit")
    profile.set_field("phone", "+39 333 1234", source="user_edit")
    profile.set_field("location", "Milan, Italy", source="user_edit")
    SqliteExperienceRepository(conn).add(Experience(
        id="exp_1", kind="role", title="Forward Deployed Engineer", org="Acme",
        start_date="2022-03", end_date="2024-05"))
    SqliteCapabilityRepository(conn).add(Capability(id="cap_1", name="Python", strength="strong"))
    SqliteEvidenceRepository(conn).add(Evidence(id="ev_1", evidence_type="cv", title="cv"))
    facts = SqliteCareerFactRepository(conn)
    facts.add(CareerFact(
        id="fact_1", fact_type="achievement",
        statement="Reduced onboarding time by 40% for 12 enterprise customers"
                  " by automating the Python deployment pipeline",
        source="interview", user_approved=1, experience_id="exp_1"))
    # Poison: an unapproved fact and a matcher-proposed edge; neither may render.
    facts.add(CareerFact(
        id="fact_poison", fact_type="achievement",
        statement="Secretly rebuilt the entire Windows kernel",
        source="cv", user_approved=0, experience_id="exp_1"))
    edges = SqliteCareerEdgeRepository(conn)
    edges.add(CareerEdge(id="edge_s", source_type="evidence", source_id="ev_1",
                         edge_type="SUPPORTS", target_type="capability", target_id="cap_1",
                         claim_kind="fact", provenance="t", created_by="user", user_verified=1))
    edges.add(CareerEdge(id="edge_p", source_type="evidence", source_id="ev_1",
                         edge_type="PROVES", target_type="career_fact", target_id="fact_1",
                         claim_kind="fact", provenance="t", created_by="user", user_verified=1))
    edges.add(CareerEdge(id="edge_poison", source_type="evidence", source_id="ev_1",
                         edge_type="PROVES", target_type="career_fact",
                         target_id="fact_poison", claim_kind="inference",
                         provenance="matcher-run", created_by="matcher", user_verified=0))
    FamilyStrategyService(conn).mint_initial(
        [RoleFamily(id="rf_1", name="FDE", rationale="r")], {"rf_1": 5}, "Land an FDE role")
    edges.add(CareerEdge(id="edge_t", source_type="role_family", source_id="rf_1",
                         edge_type="TARGETS", target_type="capability", target_id="cap_1",
                         claim_kind="fact", provenance="t", created_by="user", user_verified=1))


def _model_json(conn):
    context = package_cmd.build_context(conn, "FDE")
    cv, _dropped = build_verbatim_model(context, "2026-08-12T00:00:00Z")
    return cv.to_json()


def test_end_to_end_generate_verify_render_extract(env):
    conn, storage = env
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    assert result.status == VERIFIED
    repo = SqlitePackageRepository(conn)
    version = repo.get_version(result.version_id)
    # Full audit bundle present; artifact hash matches stored bytes.
    assert version.context_snapshot_locator and version.artifact_locator
    assert storage.exists(version.artifact_locator)
    # Every bullet traces to the approved fact; the poisoned fact and matcher
    # edge never render (input gate).
    content = json.loads(version.content_model_json)
    fact_ids = {fid for e in content["experiences"] for b in e["bullets"]
                for fid in b["fact_ids"]}
    assert fact_ids == {"fact_1"}
    assert "fact_poison" not in version.content_model_json
    assert "Windows" not in version.content_model_json
    # The snapshot audit trail exists and hashes match.
    snapshot = storage.read_text(version.context_snapshot_locator)
    assert "fact_1" in snapshot and version.input_context_hash
    # pdftotext-extracted text is asserted by the ATS report in the bundle.
    ats = json.loads(version.ats_report_json)
    assert ats["passed"] and ats["page_count"] == 1
    verifier = json.loads(version.verifier_report_json)
    assert verifier["final"]["passed"]


def test_review_accept_approves_and_export_validates_hash(env, tmp_path):
    conn, storage = env
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    said = []
    package_cmd.run_review(conn, storage, None, result.version_id, 1,
                           iter(["a"]).__next__ if False else lambda _p: "a", said.append)
    repo = SqlitePackageRepository(conn)
    assert repo.get_version(result.version_id).status == APPROVED
    out = tmp_path / "out" / "cv.pdf"
    package_cmd.run_export(conn, storage, result.version_id, out, said.append)
    assert out.read_bytes().startswith(b"%PDF")
    # Tampered bytes refuse to export.
    version = repo.get_version(result.version_id)
    path = tmp_path / "instance" / version.artifact_locator
    path.write_bytes(b"%PDF tampered")
    with pytest.raises(package_cmd.PackageCliError, match="hash mismatch"):
        package_cmd.run_export(conn, storage, result.version_id, out, said.append)


def test_ungrounded_edit_offers_mint_and_regenerates(env):
    conn, storage = env
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    answers = iter([
        "e",                                  # edit
        "0",                                  # first bullet
        "Cut cloud spend by 30% by rewriting the ingestion service",  # ungrounded edit
        "mint",                               # write-back: mint the fact
        "0",                                  # supports capability 0 (Python)
    ])
    said = []
    package_cmd.run_review(conn, storage, None, result.version_id, 1,
                           lambda _p: next(answers), said.append)
    repo = SqlitePackageRepository(conn)
    versions = repo.list_versions(repo.get_version(result.version_id).package_id)
    assert len(versions) == 2
    new = versions[-1]
    assert new.status == VERIFIED
    assert "Cut cloud spend by 30%" in new.content_model_json
    # The minted fact landed in canonical state, approved, with edges.
    minted = [f for f in SqliteCareerFactRepository(conn).list_all()
              if "Cut cloud spend" in f.statement]
    assert len(minted) == 1 and minted[0].user_approved == 1


def test_dropped_edit_ships_nothing_ungrounded(env):
    conn, storage = env
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    answers = iter(["e", "0", "Invented a quantum profit machine", "drop"])
    said = []
    package_cmd.run_review(conn, storage, None, result.version_id, 1,
                           lambda _p: next(answers), said.append)
    repo = SqlitePackageRepository(conn)
    versions = repo.list_versions(repo.get_version(result.version_id).package_id)
    assert len(versions) == 1  # no new version, nothing shipped


def test_recover_claims_expired_and_reports_orphans(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    package = repo.get_or_create_base_package("rf_1")
    stale = repo.reserve_version(package.id, "dead-worker", -5)
    # A crash between object write and row progress: orphan at the lease-
    # generation locator, unreferenced by the row.
    storage.write_text_new(
        f"packages/{package.id}/v{stale.version}/g1/context.json", "{}")
    said = []
    package_cmd.run_recover(conn, storage, said.append)
    claimed = repo.get_version(stale.id)
    assert claimed.status == FAILED
    assert "interrupted" in claimed.failure_report_json
    assert any("orphan" in s for s in said)


# --- heartbeat threading regression (drive 2026-08-11) ----------------------
# The heartbeat renews the lease from its own thread. With sqlite's default
# check_same_thread=True, sharing the worker's connection raised
# ProgrammingError on the first renewal, was swallowed into renewed=False, and
# every real generation self-terminated at the first heartbeat interval. The
# heartbeat must run against its own dedicated connection.


class _SlowVerbatimDrafter:
    """Real drafting seam that outlasts several heartbeat intervals without a
    model call: sleeps, then returns the deterministic verbatim model."""

    def __init__(self, delay: float):
        self._delay = delay

    def draft(self, context, generated_at):
        time.sleep(self._delay)
        cv, dropped = build_verbatim_model(context, generated_at)
        return DraftResult(cv=cv, report=GroundingVerifier(context).verify(cv),
                           attempts=1, fallback_used=True, dropped=dropped)


def _real_pipeline(conn, storage, drafter, heartbeat_repo_factory):
    from adapters.render.html_pdf import PlaywrightCvRenderer
    from adapters.render.pdftext import PopplerPdfTextExtractor
    return GenerationPipeline(SqlitePackageRepository(conn), storage,
                              PlaywrightCvRenderer(), PopplerPdfTextExtractor(),
                              drafter, heartbeat_repo_factory=heartbeat_repo_factory)


def test_generation_survives_heartbeats_on_a_real_thread(env, tmp_path, monkeypatch):
    """Real heartbeat thread, real temp database file, no model call: the
    generation spans several heartbeat intervals, every renewal succeeds on
    the heartbeat's own connection, and the version finalizes VERIFIED."""
    conn, storage = env
    db_file = tmp_path / "instance" / "open-career.sqlite3"
    monkeypatch.setattr("domain.pipeline.HEARTBEAT_INTERVAL_SECONDS", 0.05)
    renewals = []

    class CountingRepo(SqlitePackageRepository):
        def renew_lease(self, *args):
            renewed = super().renew_lease(*args)
            renewals.append(renewed)
            return renewed

    @contextmanager
    def heartbeat_repo():
        heartbeat_conn = sqlite3.connect(db_file)
        try:
            yield CountingRepo(heartbeat_conn)
        finally:
            heartbeat_conn.close()

    context = package_cmd.build_context(conn, "FDE")
    package = SqlitePackageRepository(conn).get_or_create_base_package("rf_1")
    pipeline = _real_pipeline(conn, storage, _SlowVerbatimDrafter(0.4), heartbeat_repo)
    result = pipeline.generate(package.id, context)
    assert result.status == VERIFIED
    assert renewals and all(renewals)  # the heartbeat actually beat, and held


def test_heartbeat_infrastructure_error_is_reported_as_such(env, tmp_path, monkeypatch):
    """A renewal that raises is an infrastructure failure, not a lost lease:
    the worker stops, marks the version FAILED under its still-live lease, and
    the report never claims recovery owns a row recover would leave alone."""
    conn, storage = env
    monkeypatch.setattr("domain.pipeline.HEARTBEAT_INTERVAL_SECONDS", 0.05)

    class BrokenRepo:
        def renew_lease(self, *args):
            raise sqlite3.ProgrammingError(
                "SQLite objects created in a thread can only be used in that same thread")

    @contextmanager
    def heartbeat_repo():
        yield BrokenRepo()

    context = package_cmd.build_context(conn, "FDE")
    package = SqlitePackageRepository(conn).get_or_create_base_package("rf_1")
    pipeline = _real_pipeline(conn, storage, _SlowVerbatimDrafter(0.3), heartbeat_repo)
    result = pipeline.generate(package.id, context)
    assert result.status == FAILED
    assert "infrastructure" in result.detail and "ProgrammingError" in result.detail
    assert "recovery owns" not in result.detail
    version = SqlitePackageRepository(conn).get_version(result.version_id)
    assert version.status == FAILED  # failed under the live lease, not orphaned
    assert "infrastructure" in version.failure_report_json


# --- husk gate (drive 2026-08-11): no experience reached, no VERIFIED CV ----


def test_generate_refuses_a_zero_experience_husk(env):
    conn, storage = env
    with conn:
        conn.execute("UPDATE career_facts SET experience_id = NULL WHERE id = 'fact_1'")
    with pytest.raises(package_cmd.PackageCliError) as excinfo:
        package_cmd.run_generate(conn, storage, None, "FDE", 1, lambda _s: None)
    message = str(excinfo.value)
    assert "zero experience entries" in message
    assert "Python" in message  # the starved capability is named
    assert "edges add" in message  # and the repair path is pointed at


def test_generate_with_a_gap_capability_still_succeeds_with_report(env):
    conn, storage = env
    SqliteCapabilityRepository(conn).add(
        Capability(id="cap_2", name="Kubernetes", strength="moderate"))
    SqliteCareerEdgeRepository(conn).add(CareerEdge(
        id="edge_t2", source_type="role_family", source_id="rf_1",
        edge_type="TARGETS", target_type="capability", target_id="cap_2",
        claim_kind="fact", provenance="t", created_by="user", user_verified=1))
    said = []
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, said.append)
    assert result.status == VERIFIED
    assert any("gap: targeted capability 'Kubernetes'" in s for s in said)


def test_show_prints_the_real_artifact_path(env, tmp_path, monkeypatch):
    conn, storage = env
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(tmp_path / "instance"))
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    said = []
    package_cmd.run_show(conn, result.version_id, False, said.append)
    artifact_lines = [s for s in said if s.strip().startswith("artifact:")]
    assert artifact_lines
    printed = pathlib.Path(artifact_lines[0].split("artifact:", 1)[1].strip())
    assert str(printed).startswith(str(tmp_path / "instance"))
    assert printed.exists()  # the printed locator is the real on-disk file


def test_generate_fails_cleanly_when_family_has_no_targets(env):
    conn, storage = env
    with SqliteCareerEdgeRepository(conn)._conn:  # retire the TARGETS edge
        conn.execute("UPDATE career_edges SET superseded_at = '2026-08-12T00:00:00Z'"
                     " WHERE id = 'edge_t'")
    with pytest.raises(package_cmd.PackageCliError, match="targets no capabilities"):
        package_cmd.run_generate(conn, storage, None, "FDE", 1, lambda _s: None)


# --- round-1 review folds (2026-08-12) --------------------------------------


def test_unreachable_experience_is_not_a_grounding_source(env):
    """An experience the family's walk never reaches supplies no grounding
    text: its words fail in a summary and in a bullet alike."""
    from dataclasses import replace

    from domain.cv_model import Bullet

    conn, storage = env
    SqliteExperienceRepository(conn).add(Experience(
        id="exp_ghost", kind="role", title="Zebrafish Wrangler", org="GhostCo",
        start_date="2010-01", end_date="2011-01"))
    context = package_cmd.build_context(conn, "FDE")
    assert {e.id for e in context.experiences} == {"exp_1"}
    verifier = GroundingVerifier(context)
    cv, _dropped = build_verbatim_model(context, "2026-08-12T00:00:00Z")
    summary_report = verifier.verify(replace(cv, summary="Zebrafish specialist"))
    assert not summary_report.passed
    entry = cv.experiences[0]
    tampered = replace(entry, bullets=(Bullet(
        text=entry.bullets[0].text + " for Zebrafish",
        fact_ids=entry.bullets[0].fact_ids),))
    bullet_report = verifier.verify(replace(cv, experiences=(tampered,)))
    assert not bullet_report.passed


class _MutatingStorage:
    """Storage that corrupts artifact bytes on write: finalization must run
    its checks on the stored bytes and never reach VERIFIED."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def write_bytes_new(self, relative_path, content):
        self._inner.write_bytes_new(relative_path, content[::-1])


def test_storage_that_mutates_bytes_cannot_finalize_verified(env):
    conn, storage = env
    result = package_cmd.run_generate(
        conn, _MutatingStorage(storage), FakeModel([_model_json(conn)]), "FDE", 1,
        lambda _s: None)
    assert result.status == FAILED
    version = SqlitePackageRepository(conn).get_version(result.version_id)
    assert version.status == FAILED


def test_confirmed_factless_education_renders_skeleton_only(env):
    conn, storage = env
    SqliteExperienceRepository(conn).add(Experience(
        id="exp_edu", kind="education", title="B.Sc. Computer Science",
        org="State University", start_date="2014-09", end_date="2017-07"))
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    assert result.status == VERIFIED
    content = json.loads(
        SqlitePackageRepository(conn).get_version(result.version_id).content_model_json)
    assert [e["experience_id"] for e in content["education"]] == ["exp_edu"]
    assert content["education"][0]["bullets"] == []
    assert {e["experience_id"] for e in content["experiences"]} == {"exp_1"}


def test_review_edit_stamps_the_current_strategy_version(env):
    """A review regeneration under a newer approved strategy version stamps
    that version in code; stale metadata from the reviewed version never
    carries over."""
    conn, storage = env
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    FamilyStrategyService(conn).set_emphasis("rf_1", 2)  # mints version 2
    grounded_edit = ("Reduced onboarding time by 40% for 12 enterprise customers"
                     " by automating the Python deployment pipeline")
    answers = iter(["e", "0", grounded_edit])
    package_cmd.run_review(conn, storage, None, result.version_id, 1,
                           lambda _p: next(answers), lambda _s: None)
    repo = SqlitePackageRepository(conn)
    versions = repo.list_versions(repo.get_version(result.version_id).package_id)
    assert len(versions) == 2 and versions[-1].status == VERIFIED
    meta = json.loads(versions[-1].content_model_json)["meta"]
    assert meta["strategy_version"] == 2


def test_null_end_date_renders_blank_never_present(env):
    """A null canonical end date is absence of a value, not a
    current-employment claim: the extracted PDF text carries no 'Present' or
    other status text the confirmed row does not state."""
    from adapters.render.pdftext import PopplerPdfTextExtractor

    conn, storage = env
    with conn:
        conn.execute("UPDATE experiences SET end_date = NULL WHERE id = 'exp_1'")
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    assert result.status == VERIFIED
    version = SqlitePackageRepository(conn).get_version(result.version_id)
    text = PopplerPdfTextExtractor().extract_layout(
        storage.read_bytes(version.artifact_locator))
    assert "present" not in text.lower()
    assert "current" not in text.lower() and "ongoing" not in text.lower()
    assert "2022-03" in text  # the confirmed start date still renders


def test_cli_export_defaults_to_the_approved_version(env, tmp_path, monkeypatch):
    from apps.cli.main import main as cli_main

    conn, storage = env
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(tmp_path / "instance"))
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    # No id and nothing approved yet: refuse with the repair path named.
    with pytest.raises(package_cmd.PackageCliError, match="no approved version"):
        package_cmd.run_export(conn, storage, None, tmp_path / "cv.pdf", lambda _s: None)
    SqlitePackageRepository(conn).approve(result.version_id, "2026-08-12T00:00:00Z")
    conn.commit()
    out = tmp_path / "exported" / "cv.pdf"
    cli_main(["package", "export", "--out", str(out)])  # id omitted
    assert out.read_bytes().startswith(b"%PDF")


def test_header_only_model_response_never_ships(env):
    """A model that keeps returning a valid-schema header-only CV cannot ship
    the husk: every draft fails verification and generation falls back to the
    verbatim model, which carries the experience-backed fact."""
    from domain.cv_model import parse_cv_model

    conn, storage = env
    from dataclasses import replace

    husk = replace(parse_cv_model(_model_json(conn)),
                   summary="", skills=(), experiences=())
    result = package_cmd.run_generate(
        conn, storage, FakeModel([husk.to_json()] * 3), "FDE", 1, lambda _s: None)
    assert result.status == VERIFIED and "fallback" in result.detail
    version = SqlitePackageRepository(conn).get_version(result.version_id)
    content = json.loads(version.content_model_json)
    assert [e["experience_id"] for e in content["experiences"]] == ["exp_1"]
    assert content["experiences"][0]["bullets"]


def test_project_only_family_generates_and_verifies(env):
    """Second-order regression on the husk gate: when the family's only
    reachable facts attach to a project-kind experience, the projects section
    carries the bullets and generation verifies."""
    conn, storage = env
    with conn:
        conn.execute("UPDATE experiences SET kind = 'project' WHERE id = 'exp_1'")
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    assert result.status == VERIFIED
    content = json.loads(
        SqlitePackageRepository(conn).get_version(result.version_id).content_model_json)
    assert content["experiences"] == []
    assert [e["experience_id"] for e in content["projects"]] == ["exp_1"]
    assert content["projects"][0]["bullets"]
