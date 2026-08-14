"""CLI inspection (`show`, `profile show`), value-validation errors through
`profile set`, and the invalid-instance guard."""

import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteCareerGoalRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
)
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from apps.cli.main import main
from domain.edges import CareerEdge
from domain.entities import Capability, CareerFact, CareerGoal, Evidence, Experience


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(tmp_path))
    migrate(tmp_path / "open-career.sqlite3")
    return tmp_path


def _seed(instance):
    conn = sqlite3.connect(instance / "open-career.sqlite3")
    try:
        SqliteExperienceRepository(conn).add(Experience(
            id="exp_1", kind="role", title="Backend Engineer", org="Acme",
            start_date="2021", end_date=None))
        SqliteCareerFactRepository(conn).add(CareerFact(
            id="fact_1", fact_type="achievement", statement="Built the order service",
            source="cv", user_approved=1, experience_id="exp_1"))
        SqliteCareerFactRepository(conn).add(CareerFact(
            id="fact_draft", fact_type="scope", statement="Unapproved draft",
            source="cv", user_approved=0, experience_id="exp_1"))
        SqliteEvidenceRepository(conn).add(Evidence(id="ev_1", evidence_type="cv", title="cv.txt"))
        SqliteCapabilityRepository(conn).add(Capability(id="cap_1", name="python", strength="strong"))
        SqliteCareerGoalRepository(conn).add(CareerGoal(
            id="goal_1", statement="Land a staff role", horizon="mid"))
        SqliteCareerEdgeRepository(conn).add(CareerEdge(
            id="edge_1", source_type="evidence", source_id="ev_1", edge_type="PROVES",
            target_type="career_fact", target_id="fact_1", claim_kind="fact",
            provenance="test", created_by="user", user_verified=1))
        SqliteUserProfileRepository(conn).set_field("full_name", "Jane Placeholder", source="user_edit")
    finally:
        conn.close()


def test_show_prints_the_stored_state(instance, capsys):
    _seed(instance)
    main(["show"])
    out = capsys.readouterr().out
    assert "full_name: Jane Placeholder" in out
    assert "[role] Backend Engineer @ Acme (2021 - present)" in out
    assert "- Built the order service" in out
    assert "Unapproved draft" not in out  # only approved facts are shown
    assert "python (strong)" in out
    assert "[mid] Land a staff role" in out
    assert "Edges: 1 total, 1 active, 0 untyped" in out
    assert "fact_1" not in out  # statements, not ids


def test_show_on_empty_instance(instance, capsys):
    main(["show"])
    out = capsys.readouterr().out
    assert "(empty)" in out and "(none)" in out
    assert "Edges: 0 total" in out


def test_profile_show_prints_only_the_profile(instance, capsys):
    _seed(instance)
    main(["profile", "show"])
    out = capsys.readouterr().out
    assert "full_name: Jane Placeholder" in out
    assert "Experiences" not in out and "Capabilities" not in out


def test_profile_set_rejects_bad_email_with_one_line(instance, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["profile", "set", "email", "garbage"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("profile set failed: 'garbage' does not look like an email address")
    assert "Traceback" not in err


def test_locked_db_gets_operational_wording_not_invalid_instance(instance, monkeypatch, capsys):
    """A locked database is a database operation failure, not an invalid
    instance: the wording must not tell the user their data is malformed.
    (WAL means a reader is no longer blocked by a writer at all, so the
    locked error is raised at the connection boundary here.)"""
    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("apps.cli.main.sqlite_connect", locked)
    with pytest.raises(SystemExit) as exc:
        main(["edges", "list"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("database operation failed: database is locked")
    assert "does not look like a valid open-career instance" not in err
    assert "Traceback" not in err


def test_instance_connections_are_wal_and_readers_are_not_blocked(instance, capsys):
    """Defect 3: the instance database runs in WAL with a busy timeout, so a
    second session holding a write transaction no longer aborts other work.
    WAL is verified in effect, never assumed."""
    from adapters.storage.sqlite_conn import BUSY_TIMEOUT_MS, connect, journal_mode

    db = instance / "open-career.sqlite3"
    conn = connect(db)
    try:
        assert journal_mode(conn) == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        writer = connect(db)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO schema_migrations (version)"
                           " VALUES ('9999_write_lock_holder')")
            # The reader runs to completion while the writer holds its
            # transaction: this is the contention that killed a 54 minute run.
            main(["edges", "list"])
        finally:
            writer.rollback()
            writer.close()
    finally:
        conn.close()
    assert "Traceback" not in capsys.readouterr().err


def test_invalid_instance_shape_is_one_line_error(tmp_path, monkeypatch, capsys):
    """A db file that is not an open-career instance (tables present, no
    schema_migrations) fails with one line, not a raw sqlite3 traceback."""
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(tmp_path))
    conn = sqlite3.connect(tmp_path / "open-career.sqlite3")
    with conn:
        conn.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    conn.close()
    with pytest.raises(SystemExit) as exc:
        main(["edges", "list"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("this does not look like a valid open-career instance (")
    assert "Traceback" not in err


def test_a_database_without_wal_fails_with_one_line_not_a_traceback(instance,
                                                                    monkeypatch,
                                                                    capsys):
    """Codex r5: refusing a database that cannot do WAL is an operational
    error at every surface, not an uncaught traceback."""
    from adapters.storage import sqlite_conn

    monkeypatch.setattr(sqlite_conn, "apply_pragmas", lambda _conn: "delete")
    with pytest.raises(SystemExit) as exc:
        main(["edges", "list"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("database operation failed: WAL journal mode did not")
    assert "Traceback" not in err
