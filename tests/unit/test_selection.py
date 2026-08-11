"""Family evidence selection: the walk and the input gate (spec:
decisions/package-generation-design.md, "Evidence selection")."""

import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
)
from domain.edges import CareerEdge
from domain.entities import Capability, CareerFact, Evidence, Experience
from domain.selection import FamilyEvidenceSelection
from domain.traversal import EvidenceTraversal


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


def _seed(conn, fact_approved=1, edge_created_by="user", edge_verified=1,
          edge_claim="fact"):
    with conn:
        conn.execute("INSERT INTO role_families (id, name, rationale) VALUES ('rf_1', 'FDE', 'r')")
    SqliteExperienceRepository(conn).add(Experience(id="exp_1", kind="role", title="Engineer"))
    SqliteCapabilityRepository(conn).add(Capability(id="cap_1", name="python", strength="strong"))
    SqliteCapabilityRepository(conn).add(Capability(id="cap_2", name="rust", strength="weak"))
    SqliteEvidenceRepository(conn).add(Evidence(id="ev_1", evidence_type="cv", title="cv"))
    SqliteCareerFactRepository(conn).add(CareerFact(
        id="fact_1", fact_type="achievement", statement="Did things",
        source="interview", user_approved=fact_approved, experience_id="exp_1"))
    edges = SqliteCareerEdgeRepository(conn)
    for eid, src, et, tgt in (
            ("edge_t1", ("role_family", "rf_1"), "TARGETS", ("capability", "cap_1")),
            ("edge_t2", ("role_family", "rf_1"), "TARGETS", ("capability", "cap_2")),
            ("edge_s", ("evidence", "ev_1"), "SUPPORTS", ("capability", "cap_1")),
            ("edge_p", ("evidence", "ev_1"), "PROVES", ("career_fact", "fact_1"))):
        created_by, verified, claim = ("user", 1, "fact")
        if eid == "edge_p":
            created_by, verified, claim = (edge_created_by, edge_verified, edge_claim)
        edges.add(CareerEdge(
            id=eid, source_type=src[0], source_id=src[1], edge_type=et,
            target_type=tgt[0], target_id=tgt[1], claim_kind=claim,
            provenance="test", created_by=created_by, user_verified=verified))
    return edges


def _selection(conn):
    edges = SqliteCareerEdgeRepository(conn)
    traversal = EvidenceTraversal(
        edges, SqliteEvidenceRepository(conn), SqliteCareerFactRepository(conn),
        SqliteExperienceRepository(conn))
    return FamilyEvidenceSelection(edges, SqliteCapabilityRepository(conn), traversal)


def test_full_walk_reaches_fact_and_reports_gap(conn):
    _seed(conn)
    report = _selection(conn).select("rf_1")
    assert report.fact_ids() == {"fact_1"}
    assert report.facts_for_capability("cap_1") == {"fact_1"}
    assert report.facts_for_capability("cap_2") == frozenset()
    assert [c.id for c in report.gaps] == ["cap_2"]  # surfaced, never papered over
    assert [s.capability.id for s in report.covered] == ["cap_1"]


def test_unapproved_fact_never_reaches_selection(conn):
    _seed(conn, fact_approved=0)
    report = _selection(conn).select("rf_1")
    assert report.fact_ids() == frozenset()
    assert {c.id for c in report.gaps} == {"cap_1", "cap_2"}


def test_matcher_edge_is_excluded_until_verified(conn):
    _seed(conn, edge_created_by="matcher", edge_verified=0, edge_claim="inference")
    report = _selection(conn).select("rf_1")
    assert report.fact_ids() == frozenset()


def test_retracted_fact_never_reaches_selection(conn):
    _seed(conn)
    SqliteCareerFactRepository(conn).set_status("fact_1", "retracted")
    assert _selection(conn).select("rf_1").fact_ids() == frozenset()
