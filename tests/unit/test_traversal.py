"""The load-bearing traversal on seeded data: capability -> eligible evidence
-> approved active facts -> experiences, with the exclusions the contract
requires (matcher-unverified edges, 'unknown'-typed migrated edges,
unapproved or non-active facts)."""

import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
)
from domain.edges import CareerEdge
from domain.entities import CareerFact, Evidence, Experience
from domain.traversal import STORY_NOTE_PREFIX, EvidenceTraversal, evidence_depth


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    yield conn
    conn.close()


def _seed(conn):
    """A capability backed by two evidence items, one with a fact chain; plus a
    matcher proposal, an unknown-typed migrated edge, and ineligible facts."""
    edges = SqliteCareerEdgeRepository(conn)
    evidence = SqliteEvidenceRepository(conn)
    facts = SqliteCareerFactRepository(conn)
    experiences = SqliteExperienceRepository(conn)

    with conn:
        conn.execute("INSERT INTO capabilities (id, name, strength) VALUES ('cap_1', 'backend', 'strong')")

    experiences.add(Experience(id="exp_1", kind="role", title="Backend Engineer", org="Acme"))
    evidence.add(Evidence(id="ev_cv", evidence_type="cv", title="cv.txt"))
    evidence.add(Evidence(id="ev_repo", evidence_type="repository", title="github repo"))
    evidence.add(Evidence(id="ev_matcher", evidence_type="url", title="matcher-found page"))

    facts.add(CareerFact(id="fact_ok", fact_type="achievement", statement="Built the order service",
                         source="cv", user_approved=1, experience_id="exp_1", verified_at="2026-08-11T00:00:00Z"))
    facts.add(CareerFact(id="fact_draft", fact_type="achievement", statement="Unapproved draft",
                         source="cv", user_approved=0, experience_id="exp_1"))
    facts.add(CareerFact(id="fact_retracted", fact_type="achievement", statement="Retracted",
                         source="cv", user_approved=1, status="retracted", experience_id="exp_1"))

    def edge(id, source_id, edge_type, target_id, target_type, **kw):
        values = dict(id=id, source_type="evidence", source_id=source_id, edge_type=edge_type,
                      target_type=target_type, target_id=target_id, claim_kind="fact",
                      provenance="test", created_by="user", user_verified=1)
        values.update(kw)
        edges.add(CareerEdge(**values))

    edge("edge_s1", "ev_cv", "SUPPORTS", "cap_1", "capability", confidence=0.9)
    edge("edge_s2", "ev_repo", "SUPPORTS", "cap_1", "capability", user_verified=0, claim_kind="fact")
    # Matcher proposal: visible in review surfaces, never traversed for generation.
    edge("edge_matcher", "ev_matcher", "SUPPORTS", "cap_1", "capability",
         created_by="matcher", user_verified=0, claim_kind="inference")
    edge("edge_p1", "ev_cv", "PROVES", "fact_ok", "career_fact")
    edge("edge_p2", "ev_cv", "PROVES", "fact_draft", "career_fact")
    edge("edge_p3", "ev_cv", "PROVES", "fact_retracted", "career_fact")

    # A migrated 0001 edge: endpoint types 'unknown', excluded from traversal.
    # Inserted directly, as the migration does; the repository would reject it.
    with conn:
        conn.execute(
            "INSERT INTO career_edges (id, source_type, source_id, edge_type, target_type,"
            " target_id, claim_kind, provenance, created_by, user_verified)"
            " VALUES ('edge_legacy', 'unknown', 'ev_cv', 'SUPPORTS', 'unknown', 'cap_1',"
            " 'fact', 'hand-import', 'import', 0)")

    return EvidenceTraversal(edges, evidence, facts, experiences)


def test_traversal_returns_eligible_chains_in_order(conn):
    traversal = _seed(conn)
    chains = traversal.evidence_for_capability("cap_1")

    # ev_matcher (unverified matcher proposal) and edge_legacy (unknown-typed)
    # are excluded; ev_cv (verified) orders before ev_repo (eligible via
    # user+fact, unverified).
    assert [c.evidence.id for c in chains] == ["ev_cv", "ev_repo"]

    cv_chain = chains[0]
    assert cv_chain.supports_edge.id == "edge_s1"
    # Only the approved, active fact survives; draft and retracted are excluded.
    assert [fc.fact.id for fc in cv_chain.facts] == ["fact_ok"]
    assert cv_chain.facts[0].experience.title == "Backend Engineer"
    assert chains[1].facts == ()


def test_matcher_edge_becomes_traversable_once_verified(conn):
    traversal = _seed(conn)
    with conn:
        conn.execute("UPDATE career_edges SET user_verified = 1 WHERE id = 'edge_matcher'")
    chains = traversal.evidence_for_capability("cap_1")
    assert "ev_matcher" in [c.evidence.id for c in chains]


def test_unknown_typed_migrated_edge_stays_excluded_but_listed(conn):
    traversal = _seed(conn)
    edges = SqliteCareerEdgeRepository(conn)
    assert [e.id for e in edges.list_untyped()] == ["edge_legacy"]
    assert "edge_legacy" not in [
        c.supports_edge.id for c in traversal.evidence_for_capability("cap_1")]


def test_unknown_capability_yields_no_chains(conn):
    traversal = _seed(conn)
    assert traversal.evidence_for_capability("cap_missing") == []


def _story(conn, evidence_id, experience_id):
    """A story bank evidence row: user_statement carrying the story note prefix."""
    SqliteEvidenceRepository(conn).add(Evidence(
        id=evidence_id, evidence_type="user_statement",
        title="story: Backend Engineer",
        notes=f"{STORY_NOTE_PREFIX}{experience_id}"))


def test_evidence_depth_counts_facts_and_stories(conn):
    """Two approved facts reached through one evidence row plus one story
    supporting the capability read as 2 and 1."""
    traversal = _seed(conn)
    facts = SqliteCareerFactRepository(conn)
    facts.add(CareerFact(id="fact_ok2", fact_type="scope", statement="Led the team",
                         source="cv", user_approved=1, experience_id="exp_1",
                         verified_at="2026-08-11T00:00:00Z"))
    edges = SqliteCareerEdgeRepository(conn)
    _story(conn, "ev_story", "exp_1")

    def edge(id, source_id, edge_type, target_id, target_type):
        edges.add(CareerEdge(
            id=id, source_type="evidence", source_id=source_id, edge_type=edge_type,
            target_type=target_type, target_id=target_id, claim_kind="fact",
            provenance="test", created_by="user", user_verified=1))

    edge("edge_p4", "ev_cv", "PROVES", "fact_ok2", "career_fact")
    edge("edge_s3", "ev_story", "SUPPORTS", "cap_1", "capability")

    depth = evidence_depth(traversal.evidence_for_capability("cap_1"))
    assert (depth.supporting_facts, depth.supporting_stories) == (2, 1)


def test_evidence_depth_is_zero_without_approved_active_facts(conn):
    """Retracted and unapproved facts back nothing: the capability's own
    evidence rows exist, and the depth still reads 0."""
    traversal = _seed(conn)
    with conn:
        conn.execute("UPDATE career_facts SET status = 'retracted' WHERE id = 'fact_ok'")
    depth = evidence_depth(traversal.evidence_for_capability("cap_1"))
    assert (depth.supporting_facts, depth.supporting_stories) == (0, 0)


def test_evidence_depth_does_not_double_count_across_chains(conn):
    """One fact proven by two evidence rows, both supporting the capability,
    is one fact of depth, not two."""
    traversal = _seed(conn)
    edges = SqliteCareerEdgeRepository(conn)
    edges.add(CareerEdge(
        id="edge_p_dup", source_type="evidence", source_id="ev_repo",
        edge_type="PROVES", target_type="career_fact", target_id="fact_ok",
        claim_kind="fact", provenance="test", created_by="user", user_verified=1))
    chains = traversal.evidence_for_capability("cap_1")
    assert sum(len(c.facts) for c in chains) == 2  # the same fact, twice over
    assert evidence_depth(chains).supporting_facts == 1
