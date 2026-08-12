"""`open-career edges add`: guarded interactive edge repair. Endpoint types
come from the vocabulary; the repository validates endpoint existence and
duplicates; the edge lands user-created, user-verified, generation-eligible
(the drive's starvation repair path when SUPPORTS links are missing)."""

import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteEvidenceRepository,
)
from apps.cli.main import run_edges_add
from domain.entities import Capability, Evidence


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    SqliteEvidenceRepository(conn).add(Evidence(id="ev_1", evidence_type="cv", title="cv"))
    SqliteCapabilityRepository(conn).add(Capability(id="cap_1", name="Python", strength="strong"))
    yield conn
    conn.close()


def _script(answers):
    it = iter(answers)
    return lambda _prompt: next(it)


def test_add_supports_edge_user_verified_and_eligible(conn):
    said = []
    run_edges_add(conn, _script(["SUPPORTS", "ev_1", "cap_1"]), said.append)
    edges = SqliteCareerEdgeRepository(conn).active_edges_to(
        "capability", "cap_1", "SUPPORTS")
    assert len(edges) == 1
    edge = edges[0]
    assert (edge.source_id, edge.created_by, edge.user_verified) == ("ev_1", "user", 1)
    assert edge.claim_kind == "fact"
    assert any("added edge" in s for s in said)


def test_add_rejects_unknown_edge_type(conn, capsys):
    with pytest.raises(SystemExit):
        run_edges_add(conn, _script(["FROBNICATES", "a", "b"]), lambda _s: None)
    assert "unknown edge type" in capsys.readouterr().err
    assert SqliteCareerEdgeRepository(conn).list_all() == []


def test_add_rejects_missing_endpoint_cleanly(conn, capsys):
    with pytest.raises(SystemExit):
        run_edges_add(conn, _script(["SUPPORTS", "ev_nope", "cap_1"]), lambda _s: None)
    assert "does not exist" in capsys.readouterr().err
    assert SqliteCareerEdgeRepository(conn).list_all() == []


def test_add_rejects_duplicate_active_edge(conn, capsys):
    run_edges_add(conn, _script(["SUPPORTS", "ev_1", "cap_1"]), lambda _s: None)
    with pytest.raises(SystemExit):
        run_edges_add(conn, _script(["SUPPORTS", "ev_1", "cap_1"]), lambda _s: None)
    assert "already exists" in capsys.readouterr().err


def test_add_aborts_cleanly_on_eof(conn, capsys):
    def eof(_prompt):
        raise EOFError
    with pytest.raises(SystemExit):
        run_edges_add(conn, eof, lambda _s: None)
    assert "nothing persisted" in capsys.readouterr().err
    assert SqliteCareerEdgeRepository(conn).list_all() == []
