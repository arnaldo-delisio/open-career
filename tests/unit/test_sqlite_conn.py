"""The instance connection boundary: WAL and busy timeout applied once where
connections are created, and a bounded retry for the transient locked error
(defect: a 54 minute discovery run died on `database is locked` while another
session held the shared instance database)."""

import sqlite3

import pytest

from adapters.storage.sqlite_conn import (
    BUSY_TIMEOUT_MS,
    apply_pragmas,
    connect,
    is_locked_error,
    journal_mode,
    retry_on_locked,
)


def test_connect_applies_wal_busy_timeout_and_foreign_keys(tmp_path):
    conn = connect(tmp_path / "db.sqlite3")
    try:
        assert journal_mode(conn) == "wal"  # verified, never assumed
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_apply_pragmas_on_an_existing_connection(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    try:
        apply_pragmas(conn)
        assert journal_mode(conn) == "wal"
    finally:
        conn.close()


def test_is_locked_error_only_matches_contention():
    assert is_locked_error(sqlite3.OperationalError("database is locked"))
    assert is_locked_error(sqlite3.OperationalError("database table is locked"))
    assert not is_locked_error(sqlite3.OperationalError("no such table: x"))
    assert not is_locked_error(sqlite3.IntegrityError("database is locked"))


def test_retry_on_locked_retries_the_whole_transaction_then_succeeds():
    attempts = []
    delays = []

    def operation():
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "committed"

    assert retry_on_locked(operation, sleep=delays.append) == "committed"
    assert len(attempts) == 3
    assert delays == [0.1, 0.2]  # bounded, doubling backoff


def test_retry_on_locked_gives_up_after_the_bound_and_raises_the_real_error():
    def operation():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        retry_on_locked(operation, attempts=3, sleep=lambda _s: None)


def test_retry_on_locked_never_retries_a_non_contention_error():
    calls = []

    def operation():
        calls.append(1)
        raise sqlite3.OperationalError("no such table: opportunities")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        retry_on_locked(operation, sleep=lambda _s: None)
    assert len(calls) == 1  # a logic error is not waited out


def test_connect_refuses_a_database_where_wal_did_not_take_effect(tmp_path,
                                                                  monkeypatch):
    """Codex r4: requesting WAL is not the same as having it. When the mode
    does not switch, the boundary says so instead of promising concurrency the
    database cannot give."""
    from adapters.storage import sqlite_conn

    monkeypatch.setattr(sqlite_conn, "apply_pragmas", lambda _conn: "delete")
    with pytest.raises(sqlite_conn.JournalModeError, match="did not take effect"):
        sqlite_conn.connect(tmp_path / "db.sqlite3")


def test_an_in_memory_database_is_accepted_as_is():
    conn = connect(":memory:")
    try:
        assert journal_mode(conn) == "memory"  # no concurrency question here
    finally:
        conn.close()


def test_apply_pragmas_reports_the_effective_mode(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite3")
    try:
        assert apply_pragmas(conn) == "wal"
    finally:
        conn.close()


def test_the_wal_refusal_is_an_operational_error_every_surface_already_handles():
    """Codex r5: the refusal must reach users as the one-line database
    operation failure, not an uncaught traceback."""
    from adapters.storage.sqlite_conn import JournalModeError

    assert issubclass(JournalModeError, sqlite3.OperationalError)
    assert not is_locked_error(JournalModeError("mode is 'delete'"))  # not retried


def test_the_busy_timeout_is_set_before_wal_negotiation(tmp_path):
    """Codex r8: switching the journal mode takes a lock itself, so a
    connection opened while another session writes must already be waiting,
    not failing during its own setup."""
    db = tmp_path / "db.sqlite3"
    first = connect(db)
    try:
        first.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        first.commit()
        order = []
        real_execute = sqlite3.Connection.execute

        class Recording(sqlite3.Connection):
            def execute(self, sql, *args):
                order.append(sql)
                return real_execute(self, sql, *args)

        # Real contention during setup: the first connection holds a write
        # transaction while the second opens and runs its pragmas.
        first.execute("BEGIN IMMEDIATE")
        first.execute("INSERT INTO t (id) VALUES (1)")
        second = connect(db, factory=Recording)
        try:
            pragmas = [s for s in order if s.lower().startswith("pragma")]
            assert "busy_timeout" in pragmas[0]
            assert any("journal_mode" in s for s in pragmas[1:])
            assert second.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
        finally:
            second.close()
    finally:
        first.rollback()
        first.close()
