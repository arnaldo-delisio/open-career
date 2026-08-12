"""End-to-end package generation (spec verification plan): seeded graph ->
generate -> verify -> render -> pdftotext -> every bullet traces and every
check passes; poisoned context asserting nothing unapproved renders; the
review write-back loop; export hash validation; lease recovery."""

import json
import sqlite3

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
from domain.generation import build_verbatim_model
from domain.packages import APPROVED, FAILED, VERIFIED
from domain.pipeline import recover_expired
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


def test_generate_fails_cleanly_when_family_has_no_targets(env):
    conn, storage = env
    with SqliteCareerEdgeRepository(conn)._conn:  # retire the TARGETS edge
        conn.execute("UPDATE career_edges SET superseded_at = '2026-08-12T00:00:00Z'"
                     " WHERE id = 'edge_t'")
    with pytest.raises(package_cmd.PackageCliError, match="targets no capabilities"):
        package_cmd.run_generate(conn, storage, None, "FDE", 1, lambda _s: None)
