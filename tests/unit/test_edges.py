"""Repository-boundary enforcement for the edge layer (spec: traversal contract
and edge vocabulary in decisions/career-graph-schema.md)."""

import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.sqlite_edges import EdgeValidationError, SqliteCareerEdgeRepository
from domain.edges import CareerEdge, is_generation_eligible


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("INSERT INTO evidence (id, evidence_type, title) VALUES ('ev_1', 'cv', 'cv.txt')")
        conn.execute("INSERT INTO capabilities (id, name, strength) VALUES ('cap_1', 'python', 'strong')")
    yield conn
    conn.close()


def _edge(**overrides) -> CareerEdge:
    values = dict(
        id="edge_t", source_type="evidence", source_id="ev_1", edge_type="SUPPORTS",
        target_type="capability", target_id="cap_1", claim_kind="fact",
        provenance="test", created_by="user", user_verified=1,
    )
    values.update(overrides)
    return CareerEdge(**values)


def test_valid_edge_is_stored_with_created_at(conn):
    stored = SqliteCareerEdgeRepository(conn).add(_edge())
    assert stored.id == "edge_t"
    assert stored.created_at is not None
    assert stored.superseded_at is None


def test_unknown_edge_type_is_rejected(conn):
    with pytest.raises(EdgeValidationError, match="unknown edge_type 'CONTAINS'"):
        SqliteCareerEdgeRepository(conn).add(_edge(edge_type="CONTAINS"))


def test_endpoint_type_mismatch_is_rejected(conn):
    with pytest.raises(EdgeValidationError, match="SUPPORTS requires evidence -> capability"):
        SqliteCareerEdgeRepository(conn).add(
            _edge(source_type="capability", source_id="cap_1",
                  target_type="evidence", target_id="ev_1"))


def test_missing_endpoint_is_rejected_and_nothing_stored(conn):
    repo = SqliteCareerEdgeRepository(conn)
    with pytest.raises(EdgeValidationError, match="capability 'cap_missing' does not exist"):
        repo.add(_edge(target_id="cap_missing"))
    assert repo.list_all() == []


def test_duplicate_active_logical_edge_is_rejected(conn):
    repo = SqliteCareerEdgeRepository(conn)
    repo.add(_edge(id="edge_a"))
    with pytest.raises(EdgeValidationError, match="active edge already exists"):
        repo.add(_edge(id="edge_b"))


def test_superseded_edge_does_not_block_a_new_active_one(conn):
    repo = SqliteCareerEdgeRepository(conn)
    repo.add(_edge(id="edge_a"))
    with conn:
        conn.execute("UPDATE career_edges SET superseded_at = '2026-08-11T00:00:00Z' WHERE id = 'edge_a'")
    repo.add(_edge(id="edge_b"))  # must not raise
    assert [e.id for e in repo.active_edges_to("capability", "cap_1", "SUPPORTS")] == ["edge_b"]


def test_generation_eligibility_gate():
    assert is_generation_eligible(_edge(created_by="user", user_verified=0, claim_kind="fact"))
    assert is_generation_eligible(_edge(created_by="import", user_verified=0, claim_kind="fact"))
    assert is_generation_eligible(_edge(created_by="matcher", user_verified=1, claim_kind="inference"))
    # Matcher-inferred, unverified: a proposal, never generation-eligible.
    assert not is_generation_eligible(_edge(created_by="matcher", user_verified=0, claim_kind="inference"))
    assert not is_generation_eligible(_edge(created_by="matcher", user_verified=0, claim_kind="fact"))
    # A user-authored inference is not a fact until verified.
    assert not is_generation_eligible(_edge(created_by="user", user_verified=0, claim_kind="inference"))
